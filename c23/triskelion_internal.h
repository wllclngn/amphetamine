/* SPDX-License-Identifier: GPL-2.0 */
/*
 * triskelion kernel module — internal declarations.
 */
#ifndef TRISKELION_INTERNAL_H
#define TRISKELION_INTERNAL_H

#include <linux/types.h>
#include <linux/spinlock.h>
#include <linux/atomic.h>
#include <linux/wait.h>
#include <linux/hashtable.h>

#include "triskelion.h"

/* ── Handle table (dense array + free list) ─────────────────────────── */

struct triskelion_object {
	enum triskelion_obj_type type;
	void                    *data;    /* type-specific payload */
	refcount_t              refcnt;
};

struct triskelion_handle_table {
	struct triskelion_object *entries;
	u32                     *free_list;
	u32                      capacity;
	u32                      free_head;
	u32                      count;
	spinlock_t               lock;
};

/* ── Sync objects ───────────────────────────────────────────────────── */

struct triskelion_semaphore {
	atomic_t          count;
	u32               max_count;    /* immutable after creation */
	wait_queue_head_t wq;
};

struct triskelion_mutex {
	spinlock_t        lock;
	u32               owner_tid;
	u32               count;       /* recursion count */
	wait_queue_head_t wq;
};

struct triskelion_event {
	atomic_t          signaled;
	u32               manual_reset; /* immutable after creation */
	wait_queue_head_t wq;
};

/* ── Message queue (per-thread) ─────────────────────────────────────── */

#define TRISKELION_QUEUE_SIZE  256
#define QUEUE_HASH_BITS        8

struct triskelion_msg_queue {
	u32                       tid;
	struct hlist_node         node;
	spinlock_t                lock;
	wait_queue_head_t         wq;
	u32                       read_pos;
	u32                       write_pos;
	struct triskelion_msg     ring[TRISKELION_QUEUE_SIZE];
};

struct triskelion_queue_table {
	DECLARE_HASHTABLE(queues, QUEUE_HASH_BITS);
	spinlock_t lock;
};

/* ── Relay queue (kernel↔daemon) ────────────────────────────────────── */

struct triskelion_relay_entry {
	struct list_head        list;
	u64                     request_id;
	u32                     client_id;
	wait_queue_head_t       done_wq;
	bool                    completed;
	int                     status;
	/* Request (copied from Wine userspace) */
	u8                     *request_buf;
	u32                     request_size;
	u32                     flags;
	struct file            *req_files[TRISKELION_RELAY_MAX_FDS];
	u32                     req_fd_count;
	/* Reply (written by daemon) */
	u8                     *reply_buf;
	u32                     reply_size;
	u32                     reply_max;
	struct file            *reply_files[TRISKELION_RELAY_MAX_FDS];
	u32                     reply_fd_count;
};

struct triskelion_relay_queue {
	struct list_head        pending;    /* submitted, not yet read by daemon */
	struct list_head        in_flight;  /* read by daemon, awaiting reply */
	spinlock_t              lock;
	wait_queue_head_t       daemon_wq;  /* daemon sleeps here */
	atomic64_t              next_id;
};

/* ── Per-opener client ─────────────────────────────────────────────── */

struct triskelion_client {
	u32                     client_id;
	bool                    is_daemon;
	struct triskelion_ctx  *server_ctx; /* shared global context */
};

/* ── Global server context (ONE per module instance) ───────────────── */

struct triskelion_ctx {
	struct triskelion_handle_table handles;
	struct triskelion_queue_table  queues;
	struct triskelion_relay_queue  relay;
	spinlock_t                    lock;
	struct triskelion_client      *daemon;  /* NULL until registered */
	atomic_t                      next_client_id;
};

/* ── Handle table ops ───────────────────────────────────────────────── */

int  triskelion_handles_init(struct triskelion_handle_table *ht, u32 capacity);
void triskelion_handles_destroy(struct triskelion_handle_table *ht);
triskelion_handle_t triskelion_handle_alloc(struct triskelion_handle_table *ht,
					    enum triskelion_obj_type type,
					    void *data);
struct triskelion_object *triskelion_handle_get(struct triskelion_handle_table *ht,
					       triskelion_handle_t handle);
int  triskelion_handle_close(struct triskelion_handle_table *ht,
			     triskelion_handle_t handle);

/* ── Queue ops ──────────────────────────────────────────────────────── */

void triskelion_queues_init(struct triskelion_queue_table *qt);
void triskelion_queues_destroy(struct triskelion_queue_table *qt);
struct triskelion_msg_queue *triskelion_queue_get_or_create(
	struct triskelion_queue_table *qt, u32 tid);
int  triskelion_queue_post(struct triskelion_msg_queue *q,
			   const struct triskelion_msg *msg);
int  triskelion_queue_get(struct triskelion_msg_queue *q,
			  struct triskelion_msg *msg);

/* ── Sync ops ───────────────────────────────────────────────────────── */

int  triskelion_sync_init(void);
void triskelion_sync_exit(void);

struct triskelion_semaphore *triskelion_sem_create(u32 initial, u32 max);
int  triskelion_sem_release(struct triskelion_semaphore *sem, u32 count, u32 *prev);
void triskelion_sem_destroy(struct triskelion_semaphore *sem);

struct triskelion_mutex *triskelion_mutex_create(u32 owner_tid);
int  triskelion_mutex_release(struct triskelion_mutex *mtx, u32 tid, u32 *prev);
void triskelion_mutex_destroy(struct triskelion_mutex *mtx);

struct triskelion_event *triskelion_event_create(u32 manual_reset, u32 initial);
int  triskelion_event_set(struct triskelion_event *evt, u32 *prev);
int  triskelion_event_reset(struct triskelion_event *evt, u32 *prev);
int  triskelion_event_pulse(struct triskelion_event *evt, u32 *prev);
void triskelion_event_destroy(struct triskelion_event *evt);

/* ── Wait ───────────────────────────────────────────────────────────── */

int  triskelion_wait(struct triskelion_handle_table *ht,
		     const triskelion_handle_t *handles, u32 count,
		     bool wait_all, s64 timeout_ns, u32 *signaled);

/* ── Relay ops ──────────────────────────────────────────────────────── */

void triskelion_relay_init(struct triskelion_relay_queue *rq);
void triskelion_relay_destroy(struct triskelion_relay_queue *rq);
long triskelion_relay_submit(struct triskelion_ctx *ctx,
			     struct triskelion_client *client,
			     void __user *uarg);
ssize_t triskelion_relay_daemon_read(struct triskelion_ctx *ctx,
				     char __user *buf, size_t count);
ssize_t triskelion_relay_daemon_write(struct triskelion_ctx *ctx,
				      const char __user *buf, size_t count);
bool triskelion_relay_has_pending(struct triskelion_relay_queue *rq);
void triskelion_relay_abort_all(struct triskelion_relay_queue *rq);
int  triskelion_relay_cache_init(void);
void triskelion_relay_cache_exit(void);

/* ── Dispatch ───────────────────────────────────────────────────────── */

long triskelion_dispatch(struct triskelion_ctx *ctx,
			 struct triskelion_client *client,
			 unsigned int cmd, unsigned long arg);

#endif /* TRISKELION_INTERNAL_H */
