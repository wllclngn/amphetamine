// Worker thread — one per Wine thread.
//
// Owns the real Client (request_fd, reply_fd, wait_fd).
// Reads requests via blocking read on request_fd.
// Forwards requests to the broker via mpsc channel.
// Receives replies via oneshot channel.
// Writes replies to reply_fd.
//
// Pipe reads (BlockingPipeRead) are handled locally — the worker does
// blocking recv() on the pipe fd, avoiding the deadlock that occurs when
// a single-threaded event loop tries to service both reader and writer.

use std::os::unix::io::RawFd;
use std::sync::mpsc;

use crate::broker::BrokerMsg;
use crate::ipc::Client;
use crate::oneshot;
use crate::protocol::*;

/// Reply from the broker to the worker.
pub enum WorkerReply {
    /// Serialized reply bytes — write directly to reply_fd.
    /// pending_fd: fd to send via SCM_RIGHTS on msg_fd BEFORE writing the reply.
    /// Guarantees fd arrives before reply on the client side (same-thread ordering).
    Data { data: Vec<u8>, pending_fd: Option<(RawFd, u32)> },
    /// Deferred (Select with timeout) — write STATUS_PENDING, ntsync handles the rest.
    Deferred,
    /// State update from init_first_thread / init_thread.
    /// Worker needs reply_fd/wait_fd/pid/tid to function.
    Init {
        data: Vec<u8>,
        pid: u32,
        tid: u32,
        reply_fd: Option<RawFd>,
        wait_fd: Option<RawFd>,
        /// Fd to send to client via SCM_RIGHTS BEFORE writing the reply.
        /// Used for inproc_device ntsync fd in init_first_thread.
        pending_fd: Option<(RawFd, u32)>,  // (fd, tag)
    },
}

pub fn worker_main(
    mut client: Client,
    broker_tx: mpsc::Sender<BrokerMsg>,
) {
    let client_fd = client.fd;

    // Record the inode of our pipe fd at startup. If the kernel reuses the fd
    // number for a different file (socketpair, new pipe, etc.), the inode changes.
    // We check this before processing any data to avoid stealing from a reused fd.
    let original_ino = {
        let mut st: libc::stat = unsafe { std::mem::zeroed() };
        if unsafe { libc::fstat(client_fd, &mut st) } == 0 { st.st_ino } else { 0 }
    };

    let mut req_buf: Vec<u8> = Vec::with_capacity(512);

    loop {
        // Poll with timeout so we can check SHUTDOWN
        let mut pfd = libc::pollfd { fd: client_fd, events: libc::POLLIN, revents: 0 };
        let poll_ret = unsafe { libc::poll(&mut pfd, 1, 500) };
        if poll_ret == 0 {
            // Timeout — check shutdown
            if crate::SHUTDOWN.load(std::sync::atomic::Ordering::Relaxed) {
                break;
            }
            continue;
        } else if poll_ret < 0 {
            let errno = std::io::Error::last_os_error().raw_os_error().unwrap_or(0);
            if errno == libc::EINTR { continue; }
            break;
        }
        // POLLHUP without POLLIN = peer closed
        if pfd.revents & libc::POLLIN == 0 && pfd.revents & (libc::POLLHUP | libc::POLLERR) != 0 {
            // Close fd IMMEDIATELY to prevent kernel fd reuse race.
            // A new_thread can get the same fd number between our poll iterations.
            // Without immediate close, we'd steal data from the new connection.
            unsafe { libc::close(client.fd); }
            client.fd = -1;
            break;
        }

        // Check if the fd was recycled by the kernel (inode changed).
        // If a previous client closed this fd and it was reused for a new
        // connection, we must NOT read from it — we'd steal the new client's data.
        {
            let mut st: libc::stat = unsafe { std::mem::zeroed() };
            let current_ino = if unsafe { libc::fstat(client_fd, &mut st) } == 0 { st.st_ino } else { 0 };
            if original_ino != 0 && current_ino != original_ino {
                client.fd = -1; // don't close — it belongs to someone else now
                break;
            }
        }

        let n = client.read_into_buf();
        if n <= 0 {
            if n == 0 {
            }
            // Close fd IMMEDIATELY (same race prevention as above)
            unsafe { libc::close(client.fd); }
            client.fd = -1;
            break;
        }

        // Process all complete requests in the buffer
        while client.has_complete_request() {
            client.take_request(&mut req_buf);

            // Drain SCM_RIGHTS fds from msg_fd BEFORE sending to broker.
            // Wine sends fds on msg_fd before the request on request_fd.
            // Bundled with the request so broker puts them in the pool
            // before dispatching the handler.
            //
            // The fd and the request travel on DIFFERENT channels (msg_fd vs
            // request_fd). The fd can arrive after the request. Stock wineserver
            // handles this with a blocking receive_fd() retry loop. We use a
            // non-blocking drain first, then poll with a short timeout if the
            // request expects an fd (new_process, new_thread, alloc_file_handle).
            let mut inflight = Vec::new();
            if client.msg_fd >= 0 {
                // Non-blocking drain first
                loop {
                    let n = client.read_fds_from_msg();
                    if n <= 0 { break; }
                }
                // If nothing drained, check if this request sends an fd.
                // Several opcodes send fds via SCM_RIGHTS on msg_fd:
                //   new_process (0) — socketpair fd
                //   init_first_thread (5) — reply_fd + wait_fd
                //   init_thread (6) — reply_fd + wait_fd
                //   new_thread (108) — request_fd (when >= 0)
                // Poll briefly to catch the fd/request race when non-blocking
                // drain missed the fds.
                if client.inflight_fds.is_empty() && req_buf.len() >= 4 {
                    let opcode = i32::from_le_bytes([req_buf[0], req_buf[1], req_buf[2], req_buf[3]]);
                    let needs_fd = opcode == 0 || opcode == 5 || opcode == 6
                        || opcode == 46 // alloc_file_handle — x11drv sends display fd
                        || (opcode == 108 && req_buf.len() >= 28 && {
                            let rfd = i32::from_le_bytes([req_buf[24], req_buf[25], req_buf[26], req_buf[27]]);
                            rfd >= 0
                        });
                    if needs_fd {
                        let mut pfd = libc::pollfd {
                            fd: client.msg_fd,
                            events: libc::POLLIN,
                            revents: 0,
                        };
                        let ready = unsafe { libc::poll(&mut pfd, 1, 50) };
                        if ready > 0 {
                            loop {
                                let n = client.read_fds_from_msg();
                                if n <= 0 { break; }
                            }
                        }
                    }
                }
                while let Some(entry) = client.inflight_fds.pop_front() {
                    inflight.push(entry);
                }
            }

            // Send request to broker and wait for reply
            let (reply_tx, reply_rx) = oneshot::channel::<WorkerReply>();
            if broker_tx.send(BrokerMsg::Request {
                client_fd,
                request_buf: req_buf.clone(),
                reply_tx,
                inflight_fds: inflight,
            }).is_err() {
                return;
            }

            let reply = match reply_rx.try_recv() {
                Some(r) => r,
                None => return, // broker dropped — exit gracefully
            };
            match reply {
                WorkerReply::Data { data, pending_fd } => {
                    // Send pending fd BEFORE writing reply -- same thread, guaranteed ordering.
                    if let Some((fd, tag)) = pending_fd {
                        let n = crate::ipc::send_fd(client.msg_fd, fd, tag);
                        if n < 0 {
                            eprintln!("[worker] send_fd FAILED: msg_fd={} fd={fd} tag={tag:#x} errno={}",
                                      client.msg_fd, std::io::Error::last_os_error());
                        }
                        unsafe { libc::close(fd); }
                    }
                    let rfd = client.reply_fd.unwrap_or(client_fd);
                    write_all_to_fd(rfd, &data);
                }
                WorkerReply::Deferred => {
                    // Select with timeout: send STATUS_PENDING to reply_fd.
                    // Wine blocks on wait_fd until ntsync wake-up arrives.
                    let pending_reply = SelectReply {
                        header: ReplyHeader { error: 0x0000_0103, reply_size: 0 },
                        apc_handle: 0,
                        signaled: 0,
                    };
                    let mut buf = [0u8; 64];
                    unsafe {
                        std::ptr::copy_nonoverlapping(
                            &pending_reply as *const _ as *const u8,
                            buf.as_mut_ptr(),
                            std::mem::size_of::<SelectReply>(),
                        );
                    }
                    write_all_to_fd(client.reply_fd.unwrap_or(client_fd), &buf);
                }
                WorkerReply::Init { data, pid, tid, reply_fd, wait_fd, pending_fd } => {
                    // Update worker-side client state from init_first_thread/init_thread
                    client.process_id = pid;
                    client.thread_id = tid;
                    if let Some(rfd) = reply_fd {
                        client.reply_fd = Some(rfd);
                    }
                    if let Some(wfd) = wait_fd {
                        client.wait_fd = Some(wfd);
                    }
                    // Send pending fd (inproc_device) BEFORE writing the reply.
                    if let Some((fd, tag)) = pending_fd {
                        crate::ipc::send_fd(client.msg_fd, fd, tag);
                        unsafe { libc::close(fd); }
                    }
                    // Write the init reply to reply_fd (which we just learned)
                    write_all_to_fd(client.reply_fd.unwrap_or(client_fd), &data);
                }
            }
        }
    }

    // fd was already closed on EOF detection above (client.fd = -1).
    // Don't close msg_fd — it's shared with the broker's phantom.
    // The broker closes it when the last thread of the process dies
    // (disconnect_client → last thread → close msg_fd).
    client.msg_fd = -1; // prevent Client::Drop from closing it

    // Notify broker of disconnect
    let _ = broker_tx.send(BrokerMsg::Disconnect {
        client_fd,
        pid: client.process_id,
        tid: client.thread_id,
    });
}

/// Write all bytes to an fd (handles partial writes).
fn write_all_to_fd(fd: RawFd, data: &[u8]) {
    let mut offset = 0;
    while offset < data.len() {
        let n = unsafe {
            libc::write(fd, data[offset..].as_ptr() as *const _, data.len() - offset)
        };
        if n <= 0 { break; }
        offset += n as usize;
    }
}
