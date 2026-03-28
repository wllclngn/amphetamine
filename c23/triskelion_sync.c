// SPDX-License-Identifier: GPL-2.0
/*
 * triskelion kernel module — sync primitives.
 *
 * NT semaphore, mutex, and event implemented with kernel wait queues.
 * Same semantics as ntsync but integrated into the triskelion handle
 * table. No separate /dev/ntsync device needed.
 *
 * Events use atomic_t for signaled state — no spinlock needed.
 * Semaphores use atomic_cmpxchg for lock-free release.
 * Mutexes keep a spinlock (owner_tid + count must update together).
 * All types allocated from dedicated slab caches.
 */

#include <linux/slab.h>
#include <linux/sched.h>
#include <linux/sched/signal.h>

#include "triskelion_internal.h"

static struct kmem_cache *sem_cache;
static struct kmem_cache *mtx_cache;
static struct kmem_cache *evt_cache;

int triskelion_sync_init(void)
{
	sem_cache = kmem_cache_create("triskelion_sem",
		sizeof(struct triskelion_semaphore), 0, 0, NULL);
	if (!sem_cache)
		return -ENOMEM;

	mtx_cache = kmem_cache_create("triskelion_mtx",
		sizeof(struct triskelion_mutex), 0, 0, NULL);
	if (!mtx_cache) {
		kmem_cache_destroy(sem_cache);
		return -ENOMEM;
	}

	evt_cache = kmem_cache_create("triskelion_evt",
		sizeof(struct triskelion_event), 0, 0, NULL);
	if (!evt_cache) {
		kmem_cache_destroy(mtx_cache);
		kmem_cache_destroy(sem_cache);
		return -ENOMEM;
	}

	return 0;
}

void triskelion_sync_exit(void)
{
	kmem_cache_destroy(evt_cache);
	kmem_cache_destroy(mtx_cache);
	kmem_cache_destroy(sem_cache);
}

/* ── Semaphore ──────────────────────────────────────────────────────── */

struct triskelion_semaphore *triskelion_sem_create(u32 initial, u32 max)
{
	struct triskelion_semaphore *sem;

	if (initial > max || max == 0)
		return ERR_PTR(-EINVAL);

	sem = kmem_cache_zalloc(sem_cache, GFP_KERNEL);
	if (!sem)
		return ERR_PTR(-ENOMEM);

	atomic_set(&sem->count, initial);
	sem->max_count = max;
	init_waitqueue_head(&sem->wq);

	return sem;
}

int triskelion_sem_release(struct triskelion_semaphore *sem, u32 count, u32 *prev)
{
	int old, new;

	do {
		old = atomic_read(&sem->count);
		new = old + count;
		if ((u32)new > sem->max_count) {
			*prev = old;
			return -EOVERFLOW;
		}
	} while (atomic_cmpxchg(&sem->count, old, new) != old);

	*prev = old;
	wake_up_interruptible(&sem->wq);
	return 0;
}

void triskelion_sem_destroy(struct triskelion_semaphore *sem)
{
	wake_up_all(&sem->wq);
	kmem_cache_free(sem_cache, sem);
}

/* ── Mutex ──────────────────────────────────────────────────────────── */

struct triskelion_mutex *triskelion_mutex_create(u32 owner_tid)
{
	struct triskelion_mutex *mtx;

	mtx = kmem_cache_zalloc(mtx_cache, GFP_KERNEL);
	if (!mtx)
		return ERR_PTR(-ENOMEM);

	spin_lock_init(&mtx->lock);
	mtx->owner_tid = owner_tid;
	mtx->count = owner_tid ? 1 : 0;
	init_waitqueue_head(&mtx->wq);

	return mtx;
}

int triskelion_mutex_release(struct triskelion_mutex *mtx, u32 tid, u32 *prev)
{
	unsigned long flags;
	bool wake = false;

	spin_lock_irqsave(&mtx->lock, flags);

	if (mtx->owner_tid != tid) {
		spin_unlock_irqrestore(&mtx->lock, flags);
		return -EPERM;
	}

	*prev = mtx->count;

	if (--mtx->count == 0) {
		mtx->owner_tid = 0;
		wake = true;
	}

	spin_unlock_irqrestore(&mtx->lock, flags);

	if (wake)
		wake_up_interruptible(&mtx->wq);

	return 0;
}

void triskelion_mutex_destroy(struct triskelion_mutex *mtx)
{
	wake_up_all(&mtx->wq);
	kmem_cache_free(mtx_cache, mtx);
}

/* ── Event ──────────────────────────────────────────────────────────── */

struct triskelion_event *triskelion_event_create(u32 manual_reset, u32 initial)
{
	struct triskelion_event *evt;

	evt = kmem_cache_zalloc(evt_cache, GFP_KERNEL);
	if (!evt)
		return ERR_PTR(-ENOMEM);

	atomic_set(&evt->signaled, initial);
	evt->manual_reset = manual_reset;
	init_waitqueue_head(&evt->wq);

	return evt;
}

int triskelion_event_set(struct triskelion_event *evt, u32 *prev)
{
	*prev = atomic_xchg(&evt->signaled, 1);

	if (evt->manual_reset)
		wake_up_all(&evt->wq);
	else
		wake_up_interruptible(&evt->wq);

	return 0;
}

int triskelion_event_reset(struct triskelion_event *evt, u32 *prev)
{
	*prev = atomic_xchg(&evt->signaled, 0);
	return 0;
}

int triskelion_event_pulse(struct triskelion_event *evt, u32 *prev)
{
	*prev = atomic_xchg(&evt->signaled, 1);

	if (evt->manual_reset)
		wake_up_all(&evt->wq);
	else
		wake_up_interruptible(&evt->wq);

	atomic_set(&evt->signaled, 0);
	return 0;
}

void triskelion_event_destroy(struct triskelion_event *evt)
{
	wake_up_all(&evt->wq);
	kmem_cache_free(evt_cache, evt);
}

/* ── Wait (WaitForSingleObject / WaitForMultipleObjects) ────────────── */

/* Check if a single object is signaled. For auto-reset event and semaphore,
 * acquiring the object consumes the signal (reset event / decrement count).
 * Returns true if signaled, false otherwise. Must be called under appropriate
 * synchronization (the wait loop serializes via the wait queue). */
static bool try_acquire(struct triskelion_object *obj, u32 owner_tid)
{
	switch (obj->type) {
	case TRISKELION_OBJ_SEMAPHORE: {
		struct triskelion_semaphore *sem = obj->data;
		int old, new;

		do {
			old = atomic_read(&sem->count);
			if (old <= 0)
				return false;
			new = old - 1;
		} while (atomic_cmpxchg(&sem->count, old, new) != old);
		return true;
	}
	case TRISKELION_OBJ_MUTEX: {
		struct triskelion_mutex *mtx = obj->data;
		unsigned long flags;
		bool acquired = false;

		spin_lock_irqsave(&mtx->lock, flags);
		if (mtx->owner_tid == 0) {
			mtx->owner_tid = owner_tid;
			mtx->count = 1;
			acquired = true;
		} else if (mtx->owner_tid == owner_tid) {
			mtx->count++;
			acquired = true;
		}
		spin_unlock_irqrestore(&mtx->lock, flags);
		return acquired;
	}
	case TRISKELION_OBJ_EVENT: {
		struct triskelion_event *evt = obj->data;

		if (!atomic_read(&evt->signaled))
			return false;
		/* Auto-reset: consume signal. Manual-reset: leave it. */
		if (!evt->manual_reset)
			atomic_set(&evt->signaled, 0);
		return true;
	}
	default:
		return false;
	}
}

/* Get the wait queue for a sync object. */
static wait_queue_head_t *obj_waitqueue(struct triskelion_object *obj)
{
	switch (obj->type) {
	case TRISKELION_OBJ_SEMAPHORE:
		return &((struct triskelion_semaphore *)obj->data)->wq;
	case TRISKELION_OBJ_MUTEX:
		return &((struct triskelion_mutex *)obj->data)->wq;
	case TRISKELION_OBJ_EVENT:
		return &((struct triskelion_event *)obj->data)->wq;
	default:
		return NULL;
	}
}

int triskelion_wait(struct triskelion_handle_table *ht,
		    const triskelion_handle_t *handles, u32 count,
		    bool wait_all, s64 timeout_ns, u32 *signaled)
{
	struct triskelion_object **objs;
	wait_queue_entry_t *wq_entries;
	u32 i;
	int ret = -ETIMEDOUT;
	long timeout_jiffies;

	if (count == 0 || count > 64)
		return -EINVAL;

	objs = kmalloc_array(count, sizeof(*objs), GFP_KERNEL);
	if (!objs)
		return -ENOMEM;

	wq_entries = kmalloc_array(count, sizeof(*wq_entries), GFP_KERNEL);
	if (!wq_entries) {
		kfree(objs);
		return -ENOMEM;
	}

	/* Resolve all handles up front */
	for (i = 0; i < count; i++) {
		objs[i] = triskelion_handle_get(ht, handles[i]);
		if (!objs[i]) {
			kfree(wq_entries);
			kfree(objs);
			return -EINVAL;
		}
	}

	/* Convert timeout */
	if (timeout_ns == 0) {
		/* Poll: try once, no blocking */
		timeout_jiffies = 0;
	} else if (timeout_ns < 0) {
		/* Relative: convert ns to jiffies */
		timeout_jiffies = nsecs_to_jiffies(-timeout_ns);
		if (timeout_jiffies == 0)
			timeout_jiffies = 1;
	} else {
		/* Infinite (caller passes S64_MAX for TIMEOUT_INFINITE) */
		timeout_jiffies = MAX_SCHEDULE_TIMEOUT;
	}

	/* Register on all wait queues */
	for (i = 0; i < count; i++) {
		wait_queue_head_t *wq = obj_waitqueue(objs[i]);

		if (!wq)
			return -EINVAL;
		init_wait(&wq_entries[i]);
		add_wait_queue(wq, &wq_entries[i]);
	}

	/* Wait loop */
	for (;;) {
		set_current_state(TASK_INTERRUPTIBLE);

		if (!wait_all) {
			/* WaitAny: first signaled object wins */
			for (i = 0; i < count; i++) {
				if (try_acquire(objs[i], current->pid)) {
					*signaled = i;
					ret = 0;
					goto done;
				}
			}
		} else {
			/* WaitAll: all must be signaled simultaneously.
			 * Check all first, then acquire all. If any fails
			 * to acquire, this is a race — retry. */
			bool all_ready = true;

			for (i = 0; i < count; i++) {
				switch (objs[i]->type) {
				case TRISKELION_OBJ_SEMAPHORE:
					if (atomic_read(&((struct triskelion_semaphore *)objs[i]->data)->count) <= 0)
						all_ready = false;
					break;
				case TRISKELION_OBJ_MUTEX: {
					struct triskelion_mutex *m = objs[i]->data;
					if (m->owner_tid != 0 && m->owner_tid != current->pid)
						all_ready = false;
					break;
				}
				case TRISKELION_OBJ_EVENT:
					if (!atomic_read(&((struct triskelion_event *)objs[i]->data)->signaled))
						all_ready = false;
					break;
				default:
					all_ready = false;
					break;
				}
				if (!all_ready)
					break;
			}

			if (all_ready) {
				bool acquired_all = true;
				for (i = 0; i < count; i++) {
					if (!try_acquire(objs[i], current->pid)) {
						acquired_all = false;
						break;
					}
				}
				if (acquired_all) {
					*signaled = 0;
					ret = 0;
					goto done;
				}
				/* Race: someone grabbed an object between check
				 * and acquire. Fall through to sleep and retry. */
			}
		}

		if (timeout_jiffies == 0) {
			/* Poll mode: don't block */
			ret = -ETIMEDOUT;
			goto done;
		}

		if (signal_pending(current)) {
			ret = -EINTR;
			goto done;
		}

		if (timeout_jiffies != MAX_SCHEDULE_TIMEOUT) {
			timeout_jiffies = schedule_timeout(timeout_jiffies);
			if (timeout_jiffies == 0) {
				ret = -ETIMEDOUT;
				goto done;
			}
		} else {
			schedule();
		}
	}

done:
	__set_current_state(TASK_RUNNING);
	for (i = 0; i < count; i++) {
		wait_queue_head_t *wq = obj_waitqueue(objs[i]);
		if (wq)
			remove_wait_queue(wq, &wq_entries[i]);
	}
	kfree(wq_entries);
	kfree(objs);
	return ret;
}
