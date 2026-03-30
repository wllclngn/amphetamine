// SPDX-License-Identifier: GPL-2.0
/*
 * triskelion kernel module — main entry point.
 *
 * Registers /dev/triskelion char device. A single global server context
 * holds handle tables, message queues, and the relay queue. Each open()
 * creates a lightweight client handle pointing at the shared context.
 * Wine processes issue ioctls; the daemon uses read()/write()/poll().
 */

#include <linux/module.h>
#include <linux/miscdevice.h>
#include <linux/fs.h>
#include <linux/slab.h>
#include <linux/poll.h>

#include "triskelion.h"
#include "triskelion_internal.h"

MODULE_LICENSE("GPL");
MODULE_AUTHOR("quark");
MODULE_DESCRIPTION("triskelion — wineserver in kernel space");
MODULE_VERSION("0.2.0");

static int max_handles = 4096;
module_param(max_handles, int, 0644);
MODULE_PARM_DESC(max_handles, "Maximum handles per server context (default: 4096)");

static bool debug;
module_param(debug, bool, 0644);
MODULE_PARM_DESC(debug, "Enable verbose debug logging");

/* Global shared server context */
static struct triskelion_ctx *global_ctx;
DEFINE_MUTEX(global_lock);

/* ── File operations ───────────────────────────────────────────────── */

static int triskelion_open(struct inode *inode, struct file *file)
{
	struct triskelion_client *client;

	client = kzalloc(sizeof(*client), GFP_KERNEL);
	if (!client)
		return -ENOMEM;

	client->client_id = atomic_inc_return(&global_ctx->next_client_id);
	client->is_daemon = false;
	client->server_ctx = global_ctx;

	file->private_data = client;

	if (debug)
		pr_info("triskelion: client %u opened (pid %d)\n",
			client->client_id, current->pid);

	return 0;
}

static int triskelion_release(struct inode *inode, struct file *file)
{
	struct triskelion_client *client = file->private_data;

	if (!client)
		return 0;

	if (client->is_daemon) {
		if (debug)
			pr_info("triskelion: daemon disconnected (pid %d)\n",
				current->pid);

		mutex_lock(&global_lock);
		if (global_ctx->daemon == client)
			global_ctx->daemon = NULL;
		mutex_unlock(&global_lock);

		/* Wake all blocked Wine threads with -EIO */
		triskelion_relay_abort_all(&global_ctx->relay);
	} else {
		if (debug)
			pr_info("triskelion: client %u closed (pid %d)\n",
				client->client_id, current->pid);
	}

	kfree(client);
	return 0;
}

static long triskelion_ioctl(struct file *file, unsigned int cmd,
			     unsigned long arg)
{
	struct triskelion_client *client = file->private_data;

	if (!client || !client->server_ctx)
		return -EINVAL;

	return triskelion_dispatch(client->server_ctx, client, cmd, arg);
}

static ssize_t triskelion_read(struct file *file, char __user *buf,
			       size_t count, loff_t *ppos)
{
	struct triskelion_client *client = file->private_data;

	if (!client || !client->is_daemon)
		return -EINVAL;

	return triskelion_relay_daemon_read(client->server_ctx, buf, count);
}

static ssize_t triskelion_write(struct file *file, const char __user *buf,
				size_t count, loff_t *ppos)
{
	struct triskelion_client *client = file->private_data;

	if (!client || !client->is_daemon)
		return -EINVAL;

	return triskelion_relay_daemon_write(client->server_ctx, buf, count);
}

static __poll_t triskelion_poll(struct file *file,
				struct poll_table_struct *wait)
{
	struct triskelion_client *client = file->private_data;
	__poll_t mask = 0;

	if (!client || !client->is_daemon)
		return EPOLLERR;

	poll_wait(file, &client->server_ctx->relay.daemon_wq, wait);

	if (triskelion_relay_has_pending(&client->server_ctx->relay))
		mask |= EPOLLIN | EPOLLRDNORM;

	/* Daemon can always write replies */
	mask |= EPOLLOUT | EPOLLWRNORM;

	return mask;
}

static const struct file_operations triskelion_fops = {
	.owner          = THIS_MODULE,
	.open           = triskelion_open,
	.release        = triskelion_release,
	.unlocked_ioctl = triskelion_ioctl,
	.compat_ioctl   = triskelion_ioctl,
	.read           = triskelion_read,
	.write          = triskelion_write,
	.poll           = triskelion_poll,
};

static struct miscdevice triskelion_misc = {
	.minor = MISC_DYNAMIC_MINOR,
	.name  = TRISKELION_DEVICE_NAME,
	.fops  = &triskelion_fops,
	.mode  = 0666,
};

/* ── Module init / exit ────────────────────────────────────────────── */

static int __init triskelion_init(void)
{
	int ret;

	/* Slab caches for sync objects */
	ret = triskelion_sync_init();
	if (ret) {
		pr_err("triskelion: failed to create sync slab caches\n");
		return ret;
	}

	/* Slab cache for relay entries */
	ret = triskelion_relay_cache_init();
	if (ret) {
		pr_err("triskelion: failed to create relay slab cache\n");
		triskelion_sync_exit();
		return ret;
	}

	/* Allocate global server context */
	global_ctx = kzalloc(sizeof(*global_ctx), GFP_KERNEL);
	if (!global_ctx) {
		triskelion_relay_cache_exit();
		triskelion_sync_exit();
		return -ENOMEM;
	}

	spin_lock_init(&global_ctx->lock);
	atomic_set(&global_ctx->next_client_id, 0);
	global_ctx->daemon = NULL;

	ret = triskelion_handles_init(&global_ctx->handles, max_handles);
	if (ret) {
		kfree(global_ctx);
		triskelion_relay_cache_exit();
		triskelion_sync_exit();
		return ret;
	}

	triskelion_queues_init(&global_ctx->queues);
	triskelion_relay_init(&global_ctx->relay);

	/* Register device */
	ret = misc_register(&triskelion_misc);
	if (ret) {
		pr_err("triskelion: failed to register /dev/%s\n",
		       TRISKELION_DEVICE_NAME);
		triskelion_relay_destroy(&global_ctx->relay);
		triskelion_queues_destroy(&global_ctx->queues);
		triskelion_handles_destroy(&global_ctx->handles);
		kfree(global_ctx);
		triskelion_relay_cache_exit();
		triskelion_sync_exit();
		return ret;
	}

	pr_info("triskelion: loaded v0.2.0 (/dev/%s, max_handles=%d, relay=on)\n",
		TRISKELION_DEVICE_NAME, max_handles);
	return 0;
}

static void __exit triskelion_exit(void)
{
	misc_deregister(&triskelion_misc);

	if (global_ctx) {
		triskelion_relay_destroy(&global_ctx->relay);
		triskelion_queues_destroy(&global_ctx->queues);
		triskelion_handles_destroy(&global_ctx->handles);
		kfree(global_ctx);
		global_ctx = NULL;
	}

	triskelion_relay_cache_exit();
	triskelion_sync_exit();
	pr_info("triskelion: unloaded\n");
}

module_init(triskelion_init);
module_exit(triskelion_exit);
