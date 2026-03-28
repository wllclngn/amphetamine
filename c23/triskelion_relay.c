// SPDX-License-Identifier: GPL-2.0
/*
 * triskelion kernel module -- relay queue.
 *
 * FUSE-style relay: Wine threads submit protocol requests via ioctl,
 * block in-kernel. The userspace Rust daemon read()s requests from
 * /dev/triskelion, processes them, write()s replies. The kernel
 * unblocks the Wine thread and copies the reply back.
 *
 * Sync ops (semaphore, mutex, event, wait, messages) are handled
 * directly in-kernel and never touch the relay queue.
 */

#include <linux/slab.h>
#include <linux/uaccess.h>
#include <linux/file.h>
#include <linux/fdtable.h>
#include <linux/anon_inodes.h>

#include "triskelion.h"
#include "triskelion_internal.h"

/* Maximum relay payload: 64-byte fixed header + 64KB vararg */
#define RELAY_MAX_PAYLOAD  (64 + 65536)

static struct kmem_cache *relay_entry_cache;

/* ── Init / destroy ────────────────────────────────────────────────── */

void triskelion_relay_init(struct triskelion_relay_queue *rq)
{
	INIT_LIST_HEAD(&rq->pending);
	INIT_LIST_HEAD(&rq->in_flight);
	spin_lock_init(&rq->lock);
	init_waitqueue_head(&rq->daemon_wq);
	atomic64_set(&rq->next_id, 1);
}

void triskelion_relay_destroy(struct triskelion_relay_queue *rq)
{
	/* Abort all pending and in-flight entries */
	triskelion_relay_abort_all(rq);
}

bool triskelion_relay_has_pending(struct triskelion_relay_queue *rq)
{
	bool has;
	unsigned long flags;

	spin_lock_irqsave(&rq->lock, flags);
	has = !list_empty(&rq->pending);
	spin_unlock_irqrestore(&rq->lock, flags);
	return has;
}

/*
 * Wake all blocked Wine threads with -EIO (daemon crashed or unloading).
 */
void triskelion_relay_abort_all(struct triskelion_relay_queue *rq)
{
	struct triskelion_relay_entry *entry, *tmp;
	unsigned long flags;

	spin_lock_irqsave(&rq->lock, flags);

	list_for_each_entry_safe(entry, tmp, &rq->pending, list) {
		list_del(&entry->list);
		entry->status = -EIO;
		entry->completed = true;
		wake_up_interruptible(&entry->done_wq);
	}

	list_for_each_entry_safe(entry, tmp, &rq->in_flight, list) {
		list_del(&entry->list);
		entry->status = -EIO;
		entry->completed = true;
		wake_up_interruptible(&entry->done_wq);
	}

	spin_unlock_irqrestore(&rq->lock, flags);
}

/* ── Helper: transfer fds between processes via kernel ─────────────── */

/*
 * Grab file references from a process's fd table.
 * Stores struct file * pointers in out_files, count in *out_count.
 * Returns 0 on success, -errno on failure.
 */
static int relay_fget_fds(const __s32 *fd_nums, u32 count,
			  struct file **out_files, u32 *out_count)
{
	u32 i;

	if (count > TRISKELION_RELAY_MAX_FDS)
		return -EINVAL;

	for (i = 0; i < count; i++) {
		out_files[i] = fget(fd_nums[i]);
		if (!out_files[i]) {
			/* Rollback */
			while (i > 0) {
				i--;
				fput(out_files[i]);
				out_files[i] = NULL;
			}
			*out_count = 0;
			return -EBADF;
		}
	}
	*out_count = count;
	return 0;
}

/*
 * Install file references into the current process's fd table.
 * Writes the new fd numbers into fd_nums_out.
 * Returns 0 on success, -errno on failure.
 */
static int relay_install_fds(struct file **files, u32 count,
			     __s32 *fd_nums_out)
{
	u32 i;
	int fd;

	for (i = 0; i < count; i++) {
		fd = get_unused_fd_flags(O_CLOEXEC);
		if (fd < 0) {
			/* Can't easily rollback installed fds, but this
			 * should be extremely rare (ulimit exhaustion). */
			return fd;
		}
		get_file(files[i]); /* extra ref for the new fd */
		fd_install(fd, files[i]);
		fd_nums_out[i] = fd;
	}
	return 0;
}

static void relay_fput_fds(struct file **files, u32 count)
{
	u32 i;

	for (i = 0; i < count; i++) {
		if (files[i]) {
			fput(files[i]);
			files[i] = NULL;
		}
	}
}

/* ── Submit (Wine thread → kernel queue) ───────────────────────────── */

long triskelion_relay_submit(struct triskelion_ctx *ctx,
			     struct triskelion_client *client,
			     void __user *uarg)
{
	struct triskelion_relay_args args;
	struct triskelion_relay_entry *entry;
	unsigned long flags;
	int ret;

	if (copy_from_user(&args, uarg, sizeof(args)))
		return -EFAULT;

	if (args.request_size < 64 || args.request_size > RELAY_MAX_PAYLOAD)
		return -EINVAL;

	if (args.fd_count_in > TRISKELION_RELAY_MAX_FDS)
		return -EINVAL;

	/* Check daemon is registered */
	if (!ctx->daemon) {
		pr_warn_ratelimited("triskelion: relay with no daemon\n");
		return -ENOENT;
	}

	/* Allocate entry */
	entry = kmem_cache_zalloc(relay_entry_cache, GFP_KERNEL);
	if (!entry)
		return -ENOMEM;

	entry->request_buf = kvmalloc(args.request_size, GFP_KERNEL);
	if (!entry->request_buf) {
		kmem_cache_free(relay_entry_cache, entry);
		return -ENOMEM;
	}

	/* Copy request payload from Wine userspace */
	if (copy_from_user(entry->request_buf, args.request,
			   args.request_size)) {
		kvfree(entry->request_buf);
		kmem_cache_free(relay_entry_cache, entry);
		return -EFAULT;
	}

	entry->request_size = args.request_size;
	entry->reply_max = args.reply_max;
	entry->client_id = client->client_id;
	entry->flags = args.flags;
	entry->request_id = atomic64_inc_return(&ctx->relay.next_id);
	init_waitqueue_head(&entry->done_wq);
	entry->completed = false;
	entry->status = 0;

	/* Grab file references for FDs being sent */
	if (args.fd_count_in > 0) {
		entry->flags |= TRISKELION_RELAY_HAS_FD;
		ret = relay_fget_fds(args.fds, args.fd_count_in,
				     entry->req_files, &entry->req_fd_count);
		if (ret) {
			kvfree(entry->request_buf);
			kmem_cache_free(relay_entry_cache, entry);
			return ret;
		}
	}

	/* Allocate reply buffer */
	if (entry->reply_max > 0) {
		entry->reply_buf = kvmalloc(entry->reply_max, GFP_KERNEL);
		if (!entry->reply_buf) {
			relay_fput_fds(entry->req_files, entry->req_fd_count);
			kvfree(entry->request_buf);
			kmem_cache_free(relay_entry_cache, entry);
			return -ENOMEM;
		}
	}

	/* Enqueue and wake daemon */
	spin_lock_irqsave(&ctx->relay.lock, flags);
	list_add_tail(&entry->list, &ctx->relay.pending);
	spin_unlock_irqrestore(&ctx->relay.lock, flags);

	wake_up_interruptible(&ctx->relay.daemon_wq);

	/* Block until daemon responds or we're interrupted */
	ret = wait_event_interruptible(entry->done_wq, entry->completed);
	if (ret) {
		/*
		 * Interrupted by signal. Remove entry if still pending.
		 * If daemon already dequeued it (in_flight), we must
		 * let it complete to avoid use-after-free.
		 */
		spin_lock_irqsave(&ctx->relay.lock, flags);
		if (!entry->completed) {
			list_del(&entry->list);
			spin_unlock_irqrestore(&ctx->relay.lock, flags);
			/* Safe to free — daemon hasn't touched it */
			relay_fput_fds(entry->req_files, entry->req_fd_count);
			kvfree(entry->reply_buf);
			kvfree(entry->request_buf);
			kmem_cache_free(relay_entry_cache, entry);
			return -EINTR;
		}
		spin_unlock_irqrestore(&ctx->relay.lock, flags);
		/* Entry was completed while we were being interrupted.
		 * Fall through to copy reply. */
	}

	/* Entry completed — copy reply to Wine userspace */
	if (entry->status) {
		ret = entry->status;
		goto out_free;
	}

	if (entry->reply_size > 0 && args.reply) {
		u32 to_copy = min(entry->reply_size, args.reply_max);
		if (copy_to_user(args.reply, entry->reply_buf, to_copy)) {
			ret = -EFAULT;
			goto out_free;
		}
		args.reply_size = to_copy;
	} else {
		args.reply_size = 0;
	}

	/* Install reply FDs into Wine's fd table */
	args.fd_count_out = 0;
	if (entry->reply_fd_count > 0) {
		ret = relay_install_fds(entry->reply_files,
					entry->reply_fd_count, args.fds);
		if (ret)
			goto out_free;
		args.fd_count_out = entry->reply_fd_count;
	}

	if (copy_to_user(uarg, &args, sizeof(args))) {
		ret = -EFAULT;
		goto out_free;
	}

	ret = 0;

out_free:
	relay_fput_fds(entry->req_files, entry->req_fd_count);
	relay_fput_fds(entry->reply_files, entry->reply_fd_count);
	kvfree(entry->reply_buf);
	kvfree(entry->request_buf);
	kmem_cache_free(relay_entry_cache, entry);
	return ret;
}

/* ── Daemon read (dequeue pending request) ─────────────────────────── */

ssize_t triskelion_relay_daemon_read(struct triskelion_ctx *ctx,
				     char __user *buf, size_t count)
{
	struct triskelion_relay_queue *rq = &ctx->relay;
	struct triskelion_relay_entry *entry;
	struct triskelion_relay_header hdr;
	unsigned long flags;
	ssize_t total;
	int ret;

	/* Wait for a pending request */
	ret = wait_event_interruptible(rq->daemon_wq,
				       !list_empty(&rq->pending));
	if (ret)
		return -EINTR;

	/* Dequeue first pending entry */
	spin_lock_irqsave(&rq->lock, flags);
	if (list_empty(&rq->pending)) {
		spin_unlock_irqrestore(&rq->lock, flags);
		return -EAGAIN;
	}
	entry = list_first_entry(&rq->pending,
				 struct triskelion_relay_entry, list);
	list_move_tail(&entry->list, &rq->in_flight);
	spin_unlock_irqrestore(&rq->lock, flags);

	/* Build header */
	memset(&hdr, 0, sizeof(hdr));
	hdr.request_id = entry->request_id;
	hdr.client_id = entry->client_id;
	/* Opcode is first 4 bytes of the Wine request (little-endian i32) */
	if (entry->request_size >= 4)
		memcpy(&hdr.opcode, entry->request_buf, sizeof(hdr.opcode));
	hdr.payload_size = entry->request_size;
	hdr.flags = entry->flags;
	hdr.fd_count = entry->req_fd_count;

	total = sizeof(hdr) + entry->request_size;
	if ((size_t)total > count)
		return -ENOSPC;

	/* Install request FDs into daemon's fd table */
	if (entry->req_fd_count > 0) {
		ret = relay_install_fds(entry->req_files,
					entry->req_fd_count, hdr.fds);
		if (ret)
			return ret;
	}

	/* Copy header + payload to daemon */
	if (copy_to_user(buf, &hdr, sizeof(hdr)))
		return -EFAULT;
	if (entry->request_size > 0) {
		if (copy_to_user(buf + sizeof(hdr), entry->request_buf,
				 entry->request_size))
			return -EFAULT;
	}

	return total;
}

/* ── Daemon write (deliver reply to blocked Wine thread) ───────────── */

ssize_t triskelion_relay_daemon_write(struct triskelion_ctx *ctx,
				      const char __user *buf, size_t count)
{
	struct triskelion_relay_queue *rq = &ctx->relay;
	struct triskelion_relay_header hdr;
	struct triskelion_relay_entry *entry, *tmp;
	unsigned long flags;
	u32 reply_data_size;

	if (count < sizeof(hdr))
		return -EINVAL;

	if (copy_from_user(&hdr, buf, sizeof(hdr)))
		return -EFAULT;

	reply_data_size = count - sizeof(hdr);

	/* Find matching in-flight entry by request_id */
	spin_lock_irqsave(&rq->lock, flags);
	entry = NULL;
	list_for_each_entry_safe(tmp, entry, &rq->in_flight, list) {
		if (tmp->request_id == hdr.request_id) {
			entry = tmp;
			list_del(&entry->list);
			break;
		}
		entry = NULL;
	}
	spin_unlock_irqrestore(&rq->lock, flags);

	if (!entry)
		return -ESRCH;

	/* Copy reply data */
	if (reply_data_size > 0 && entry->reply_buf) {
		u32 to_copy = min(reply_data_size, entry->reply_max);
		if (copy_from_user(entry->reply_buf,
				   buf + sizeof(hdr), to_copy)) {
			/* Put entry back as failed */
			entry->status = -EFAULT;
			entry->completed = true;
			wake_up_interruptible(&entry->done_wq);
			return -EFAULT;
		}
		entry->reply_size = to_copy;
	}

	/* Handle reply FDs from daemon */
	if ((hdr.flags & TRISKELION_RELAY_REPLY_HAS_FD) &&
	    hdr.fd_count > 0 && hdr.fd_count <= TRISKELION_RELAY_MAX_FDS) {
		int ret = relay_fget_fds(hdr.fds, hdr.fd_count,
					 entry->reply_files,
					 &entry->reply_fd_count);
		if (ret) {
			entry->status = ret;
			entry->completed = true;
			wake_up_interruptible(&entry->done_wq);
			return ret;
		}
	}

	/* Wake the blocked Wine thread */
	entry->status = 0;
	entry->completed = true;
	wake_up_interruptible(&entry->done_wq);

	return count;
}

/* ── Module init/exit for relay slab cache ─────────────────────────── */

int __init triskelion_relay_cache_init(void)
{
	relay_entry_cache = kmem_cache_create("triskelion_relay",
		sizeof(struct triskelion_relay_entry),
		0, SLAB_HWCACHE_ALIGN, NULL);
	if (!relay_entry_cache)
		return -ENOMEM;
	return 0;
}

void triskelion_relay_cache_exit(void)
{
	if (relay_entry_cache)
		kmem_cache_destroy(relay_entry_cache);
}
