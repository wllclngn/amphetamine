// SPDX-License-Identifier: GPL-2.0
/*
 * triskelion kernel module — ioctl dispatch.
 *
 * Maps ioctl commands to handler functions. Each handler copies args
 * from userspace, operates on the server context, and copies results back.
 *
 * Sync/message/wait ops are handled directly in-kernel.
 * All other opcodes are relayed to the userspace daemon via the relay queue.
 */

#include <linux/uaccess.h>
#include <linux/slab.h>
#include <linux/mutex.h>

#include "triskelion.h"
#include "triskelion_internal.h"

extern struct mutex global_lock;

/* ── Sync object creation ───────────────────────────────────────────── */

static long do_create_sem(struct triskelion_ctx *ctx, void __user *uarg)
{
	struct triskelion_sem_args args;
	struct triskelion_semaphore *sem;
	triskelion_handle_t handle;

	if (copy_from_user(&args, uarg, sizeof(args)))
		return -EFAULT;

	sem = triskelion_sem_create(args.count, args.max_count);
	if (IS_ERR(sem))
		return PTR_ERR(sem);

	handle = triskelion_handle_alloc(&ctx->handles,
					 TRISKELION_OBJ_SEMAPHORE, sem);
	if (handle == TRISKELION_INVALID_HANDLE) {
		triskelion_sem_destroy(sem);
		return -ENOMEM;
	}

	args.handle = handle;
	if (copy_to_user(uarg, &args, sizeof(args))) {
		triskelion_handle_close(&ctx->handles, handle);
		return -EFAULT;
	}

	return 0;
}

static long do_create_mutex(struct triskelion_ctx *ctx, void __user *uarg)
{
	struct triskelion_mutex_args args;
	struct triskelion_mutex *mtx;
	triskelion_handle_t handle;

	if (copy_from_user(&args, uarg, sizeof(args)))
		return -EFAULT;

	mtx = triskelion_mutex_create(args.owner_tid);
	if (IS_ERR(mtx))
		return PTR_ERR(mtx);

	handle = triskelion_handle_alloc(&ctx->handles,
					 TRISKELION_OBJ_MUTEX, mtx);
	if (handle == TRISKELION_INVALID_HANDLE) {
		triskelion_mutex_destroy(mtx);
		return -ENOMEM;
	}

	args.handle = handle;
	if (copy_to_user(uarg, &args, sizeof(args))) {
		triskelion_handle_close(&ctx->handles, handle);
		return -EFAULT;
	}

	return 0;
}

static long do_create_event(struct triskelion_ctx *ctx, void __user *uarg)
{
	struct triskelion_event_args args;
	struct triskelion_event *evt;
	triskelion_handle_t handle;

	if (copy_from_user(&args, uarg, sizeof(args)))
		return -EFAULT;

	evt = triskelion_event_create(args.manual_reset, args.initial_state);
	if (IS_ERR(evt))
		return PTR_ERR(evt);

	handle = triskelion_handle_alloc(&ctx->handles,
					 TRISKELION_OBJ_EVENT, evt);
	if (handle == TRISKELION_INVALID_HANDLE) {
		triskelion_event_destroy(evt);
		return -ENOMEM;
	}

	args.handle = handle;
	if (copy_to_user(uarg, &args, sizeof(args))) {
		triskelion_handle_close(&ctx->handles, handle);
		return -EFAULT;
	}

	return 0;
}

/* ── Sync operations ────────────────────────────────────────────────── */

static long do_release_sem(struct triskelion_ctx *ctx, void __user *uarg)
{
	struct triskelion_sem_args args;
	struct triskelion_object *obj;
	int ret;

	if (copy_from_user(&args, uarg, sizeof(args)))
		return -EFAULT;

	obj = triskelion_handle_get(&ctx->handles, args.handle);
	if (!obj || obj->type != TRISKELION_OBJ_SEMAPHORE)
		return -EINVAL;

	ret = triskelion_sem_release(obj->data, args.count, &args.prev_count);
	if (ret)
		return ret;

	if (copy_to_user(uarg, &args, sizeof(args)))
		return -EFAULT;

	return 0;
}

static long do_release_mutex(struct triskelion_ctx *ctx, void __user *uarg)
{
	struct triskelion_mutex_args args;
	struct triskelion_object *obj;
	int ret;

	if (copy_from_user(&args, uarg, sizeof(args)))
		return -EFAULT;

	obj = triskelion_handle_get(&ctx->handles, args.handle);
	if (!obj || obj->type != TRISKELION_OBJ_MUTEX)
		return -EINVAL;

	ret = triskelion_mutex_release(obj->data, args.owner_tid,
				       &args.prev_count);
	if (ret)
		return ret;

	if (copy_to_user(uarg, &args, sizeof(args)))
		return -EFAULT;

	return 0;
}

static long do_set_event(struct triskelion_ctx *ctx, void __user *uarg)
{
	struct triskelion_event_args args;
	struct triskelion_object *obj;

	if (copy_from_user(&args, uarg, sizeof(args)))
		return -EFAULT;

	obj = triskelion_handle_get(&ctx->handles, args.handle);
	if (!obj || obj->type != TRISKELION_OBJ_EVENT)
		return -EINVAL;

	triskelion_event_set(obj->data, &args.prev_state);

	if (copy_to_user(uarg, &args, sizeof(args)))
		return -EFAULT;

	return 0;
}

static long do_reset_event(struct triskelion_ctx *ctx, void __user *uarg)
{
	struct triskelion_event_args args;
	struct triskelion_object *obj;

	if (copy_from_user(&args, uarg, sizeof(args)))
		return -EFAULT;

	obj = triskelion_handle_get(&ctx->handles, args.handle);
	if (!obj || obj->type != TRISKELION_OBJ_EVENT)
		return -EINVAL;

	triskelion_event_reset(obj->data, &args.prev_state);

	if (copy_to_user(uarg, &args, sizeof(args)))
		return -EFAULT;

	return 0;
}

static long do_pulse_event(struct triskelion_ctx *ctx, void __user *uarg)
{
	struct triskelion_event_args args;
	struct triskelion_object *obj;

	if (copy_from_user(&args, uarg, sizeof(args)))
		return -EFAULT;

	obj = triskelion_handle_get(&ctx->handles, args.handle);
	if (!obj || obj->type != TRISKELION_OBJ_EVENT)
		return -EINVAL;

	triskelion_event_pulse(obj->data, &args.prev_state);

	if (copy_to_user(uarg, &args, sizeof(args)))
		return -EFAULT;

	return 0;
}

/* ── Message queue ──────────────────────────────────────────────────── */

static long do_post_msg(struct triskelion_ctx *ctx, void __user *uarg)
{
	struct triskelion_post_msg_args args;
	struct triskelion_msg_queue *q;

	if (copy_from_user(&args, uarg, sizeof(args)))
		return -EFAULT;

	q = triskelion_queue_get_or_create(&ctx->queues, args.target_tid);
	if (IS_ERR(q))
		return PTR_ERR(q);

	return triskelion_queue_post(q, &args.msg);
}

static long do_get_msg(struct triskelion_ctx *ctx, void __user *uarg)
{
	struct triskelion_get_msg_args args = {};
	struct triskelion_msg_queue *q;
	int ret;

	q = triskelion_queue_get_or_create(&ctx->queues, current->pid);
	if (IS_ERR(q))
		return PTR_ERR(q);

	ret = triskelion_queue_get(q, &args.msg);
	args.has_message = (ret == 0) ? 1 : 0;

	if (copy_to_user(uarg, &args, sizeof(args)))
		return -EFAULT;

	return 0;
}

/* ── Wait ───────────────────────────────────────────────────────────── */

static long do_wait(struct triskelion_ctx *ctx, void __user *uarg, bool wait_all)
{
	struct triskelion_wait_args args;
	triskelion_handle_t handles[64];
	int ret;

	if (copy_from_user(&args, uarg, sizeof(args)))
		return -EFAULT;

	if (args.count == 0 || args.count > 64)
		return -EINVAL;

	if (copy_from_user(handles, (const void __user *)args.handles,
			   args.count * sizeof(triskelion_handle_t)))
		return -EFAULT;

	args.wait_all = wait_all ? 1 : 0;

	ret = triskelion_wait(&ctx->handles, handles, args.count,
			      wait_all, args.timeout_ns, &args.signaled_index);

	if (ret == -ETIMEDOUT) {
		args.signaled_index = (u32)-1;
		if (copy_to_user(uarg, &args, sizeof(args)))
			return -EFAULT;
		return -ETIMEDOUT;
	}

	if (ret)
		return ret;

	if (copy_to_user(uarg, &args, sizeof(args)))
		return -EFAULT;

	return 0;
}

/* ── Handle ops ─────────────────────────────────────────────────────── */

static long do_close(struct triskelion_ctx *ctx, void __user *uarg)
{
	triskelion_handle_t handle;

	if (copy_from_user(&handle, uarg, sizeof(handle)))
		return -EFAULT;

	return triskelion_handle_close(&ctx->handles, handle);
}

/* ── Relay + daemon registration ───────────────────────────────────── */

static long do_relay(struct triskelion_ctx *ctx,
		     struct triskelion_client *client,
		     void __user *uarg)
{
	return triskelion_relay_submit(ctx, client, uarg);
}

static long do_register_daemon(struct triskelion_ctx *ctx,
			       struct triskelion_client *client,
			       void __user *uarg)
{
	struct triskelion_daemon_args args;

	if (copy_from_user(&args, uarg, sizeof(args)))
		return -EFAULT;

	mutex_lock(&global_lock);
	if (ctx->daemon && ctx->daemon != client) {
		/* Replace stale daemon — abort its pending requests */
		pr_warn("triskelion: replacing stale daemon (old client %u)\n",
			ctx->daemon->client_id);
		ctx->daemon->is_daemon = false;
		triskelion_relay_abort_all(&ctx->relay);
	}
	client->is_daemon = true;
	ctx->daemon = client;
	mutex_unlock(&global_lock);

	pr_info("triskelion: daemon registered (pid %d, version %u)\n",
		current->pid, args.version);
	return 0;
}

/* ── Dispatch table ─────────────────────────────────────────────────── */

long triskelion_dispatch(struct triskelion_ctx *ctx,
			 struct triskelion_client *client,
			 unsigned int cmd, unsigned long arg)
{
	void __user *uarg = (void __user *)arg;

	switch (cmd) {
	/* Sync creation */
	case TRISKELION_IOC_CREATE_SEM:    return do_create_sem(ctx, uarg);
	case TRISKELION_IOC_CREATE_MUTEX:  return do_create_mutex(ctx, uarg);
	case TRISKELION_IOC_CREATE_EVENT:  return do_create_event(ctx, uarg);

	/* Sync operations */
	case TRISKELION_IOC_RELEASE_SEM:   return do_release_sem(ctx, uarg);
	case TRISKELION_IOC_RELEASE_MUTEX: return do_release_mutex(ctx, uarg);
	case TRISKELION_IOC_SET_EVENT:     return do_set_event(ctx, uarg);
	case TRISKELION_IOC_RESET_EVENT:   return do_reset_event(ctx, uarg);
	case TRISKELION_IOC_PULSE_EVENT:   return do_pulse_event(ctx, uarg);

	/* Wait */
	case TRISKELION_IOC_WAIT_ANY:      return do_wait(ctx, uarg, false);
	case TRISKELION_IOC_WAIT_ALL:      return do_wait(ctx, uarg, true);

	/* Message queue */
	case TRISKELION_IOC_POST_MSG:      return do_post_msg(ctx, uarg);
	case TRISKELION_IOC_GET_MSG:       return do_get_msg(ctx, uarg);

	/* Handle ops */
	case TRISKELION_IOC_CLOSE:         return do_close(ctx, uarg);

	/* Relay (non-sync opcodes → userspace daemon) */
	case TRISKELION_IOC_RELAY:         return do_relay(ctx, client, uarg);

	/* Daemon registration */
	case TRISKELION_IOC_REGISTER_DAEMON:
		return do_register_daemon(ctx, client, uarg);

	default:
		return -ENOTTY;
	}
}
