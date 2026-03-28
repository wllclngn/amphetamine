// Broker thread — owns all global mutable state via EventLoop.
//
// Workers communicate with the broker via mpsc channel. Each request
// includes a oneshot reply channel so the worker blocks until the
// broker responds. This is the ONLY way to access global state:
// no mutexes, no RwLocks — just message passing.
//
// The broker wraps EventLoop and reuses ALL existing handler code
// unchanged. Phantom clients in ev.clients mirror worker-owned clients
// so handlers can look up pid/tid/fds as before.

use std::os::unix::io::RawFd;
use std::sync::mpsc;
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};

use crate::event_loop::{EventLoop, Reply};
use crate::ipc::Client;
use crate::oneshot;
use crate::protocol::*;
use crate::worker::WorkerReply;
use crate::SHUTDOWN;

// ---- Broker message enum (simplified — generic dispatch) ----

pub enum BrokerMsg {
    /// Acceptor sends a freshly-accepted client.
    NewClient {
        client: Client,
        msg_fd: RawFd,
    },
    /// Worker forwards a raw protocol request.
    Request {
        client_fd: RawFd,
        request_buf: Vec<u8>,
        reply_tx: oneshot::Sender<WorkerReply>,
        /// SCM_RIGHTS fds drained by the worker from msg_fd BEFORE sending.
        /// Each entry: (thread_id, fd_number, actual_fd).
        inflight_fds: Vec<(u32, i32, RawFd)>,
    },
    /// Worker disconnected (EOF on request_fd).
    Disconnect {
        client_fd: RawFd,
        pid: u32,
        tid: u32,
    },
    /// Housekeeping tick from dedicated timer thread.
    Tick,
}

// ---- Broker state ----

pub struct BrokerState {
    pub ev: EventLoop,
    pub broker_tx: mpsc::Sender<BrokerMsg>,
    pub worker_count: usize,           // total ever created (for naming)
    pub active_workers: Arc<AtomicUsize>, // currently alive (decremented on exit)
}

// Safety: BrokerState is only accessed from the broker thread.
unsafe impl Send for BrokerState {}

// ---- Broker main loop ----

pub fn broker_main(
    rx: mpsc::Receiver<BrokerMsg>,
    mut state: BrokerState,
) {
    log_info!("broker thread started");

    // Pure message-driven loop. No timers, no polling, no timeouts.
    // The broker processes messages as they arrive — cause and effect.
    // Housekeeping (pending waits, USD time, shutdown/linger checks)
    // arrives via Tick messages from a dedicated timer thread.
    loop {
        match rx.recv() {
            Ok(msg) => {
                dispatch_broker_msg(&mut state, msg);
                // Complete any async pipe reads triggered by writes
                state.ev.check_pending_pipe_reads();
            }
            Err(mpsc::RecvError) => {
                log_info!("broker: all senders dropped, shutting down");
                break;
            }
        }
    }

    // Save registry to disk before exiting
    state.ev.registry.save_to_prefix(&state.ev.user_sid_str);
    log_info!("broker shutdown");
}

fn dispatch_broker_msg(state: &mut BrokerState, msg: BrokerMsg) {
    match msg {
        BrokerMsg::NewClient { client, msg_fd } => {
            handle_new_client(state, client, msg_fd);
        }
        BrokerMsg::Request { client_fd, request_buf, reply_tx, inflight_fds } => {
            // Put worker-drained fds into the process pool BEFORE dispatch.
            // This eliminates the SCM_RIGHTS race: the worker drained msg_fd
            // on the same causal chain as reading the request, so fds are
            // guaranteed to be here before the handler needs them.
            if !inflight_fds.is_empty() {
                let pid = state.ev.clients.get(&client_fd)
                    .map(|c| c.process_id).unwrap_or(0);
                if pid != 0 {
                    let pool = state.ev.process_inflight_fds.entry(pid).or_default();
                    for entry in inflight_fds {
                        pool.push_back(entry);
                    }
                } else {
                    // Pre-init: put in client's own inflight queue
                    if let Some(client) = state.ev.clients.get_mut(&client_fd) {
                        for entry in inflight_fds {
                            client.inflight_fds.push_back(entry);
                        }
                    }
                }
            }
            handle_request(state, client_fd, request_buf, reply_tx);
        }
        BrokerMsg::Disconnect { client_fd, pid, tid } => {
            handle_disconnect(state, client_fd, pid, tid);
        }
        BrokerMsg::Tick => {
            // Housekeeping: pending waits, USD time, shutdown/linger, queue fds
            state.ev.check_pending_pipe_reads();
            state.ev.check_pending_waits();
            state.ev.check_win_timers();
            state.ev.poll_queue_fds();
            state.ev.update_usd_time();
            if SHUTDOWN.load(std::sync::atomic::Ordering::Relaxed) {
                // Can't break from here (inside dispatch_broker_msg).
                // The sender will be dropped when SHUTDOWN is detected
                // by the timer thread or acceptor thread.
            }
            if let Some(deadline) = state.ev.linger_deadline {
                if std::time::Instant::now() >= deadline {
                    log_info!("broker: linger deadline expired, shutting down");
                    SHUTDOWN.store(true, std::sync::atomic::Ordering::Relaxed);
                }
            }
        }
    }
}

// ---- New client: create phantom, spawn worker ----

fn handle_new_client(state: &mut BrokerState, client: Client, _msg_fd: RawFd) {
    let request_fd = client.fd;
    let msg_fd = client.msg_fd;

    // Cancel linger deadline — a new client connected, so don't shut down.
    if state.ev.linger_deadline.is_some() {
        log_info!("broker: new client fd={request_fd}, cancelling linger deadline");
        state.ev.linger_deadline = None;
    }

    // Create phantom client for the broker's EventLoop.
    // Phantom shares fd values so handlers can call send_fd(client.msg_fd, ...)
    // and it reaches the real Wine process.
    let phantom = Client::phantom_from(&client);
    state.ev.clients.insert(request_fd, phantom);
    state.ev.msg_fd_map.insert(msg_fd, request_fd);

    // Worker gets the original msg_fd — it's the SOLE reader for SCM_RIGHTS.
    // Broker's phantom keeps msg_fd for send_fd() only (writing TO client).
    // No dup needed — one reader, one writer, no competition.
    let worker_client = client;
    // msg_fd stays as-is (the original from accept/socketpair)

    // Spawn worker thread
    let broker_tx = state.broker_tx.clone();
    state.worker_count += 1;
    let worker_num = state.worker_count;
    let active_counter = state.active_workers.clone();
    active_counter.fetch_add(1, Ordering::Relaxed);
    std::thread::Builder::new()
        .name(format!("w-{worker_num}"))
        .spawn(move || {
            crate::worker::worker_main(worker_client, broker_tx);
            active_counter.fetch_sub(1, Ordering::Relaxed);
        })
        .expect("spawn worker thread");

}

// ---- Request: drain msg_fd, dispatch, detect new clients ----

fn handle_request(
    state: &mut BrokerState,
    client_fd: RawFd,
    request_buf: Vec<u8>,
    reply_tx: oneshot::Sender<WorkerReply>,
) {
    if !state.ev.clients.contains_key(&client_fd) {
        reply_tx.send(WorkerReply::Data { data: vec![0u8; 64], pending_fd: None });
        return;
    }

    let header: RequestHeader = unsafe {
        std::ptr::read_unaligned(request_buf.as_ptr() as *const RequestHeader)
    };

    // Peek at opcode to detect init requests (need state sync back to worker)
    let is_init = matches!(
        RequestCode::from_i32(header.req),
        Some(RequestCode::InitFirstThread) | Some(RequestCode::InitThread)
    );

    // Track client count to detect new clients spawned by handlers
    let clients_before = state.ev.clients.len();

    // Dispatch via EventLoop — reuses ALL existing handler code unchanged
    let reply = state.ev.dispatch(client_fd, &header, &request_buf);

    // Detect new clients created by handle_new_process / handle_new_thread
    if state.ev.clients.len() > clients_before {
        spawn_new_clients(state, client_fd);
    }

    // Convert Reply to WorkerReply
    let worker_reply = if is_init {
        // For init requests, send back the updated pid/tid/reply_fd/wait_fd
        // so the worker knows where to write replies
        let (pid, tid, reply_fd, wait_fd, pending_fd) = state.ev.clients.get_mut(&client_fd)
            .map(|c| {
                let pfd = c.pending_fd.take(); // take() so it's only sent once
                (c.process_id, c.thread_id, c.reply_fd, c.wait_fd, pfd)
            })
            .unwrap_or((0, 0, None, None, None));
        WorkerReply::Init {
            data: reply_to_bytes(reply),
            pid,
            tid,
            reply_fd,
            wait_fd,
            pending_fd,
        }
    } else {
        // Extract pending_fd from phantom -- handlers store fds here
        // instead of sending directly on msg_fd (prevents race condition).
        let pending_fd = state.ev.clients.get_mut(&client_fd)
            .and_then(|c| c.pending_fd.take());
        match reply {
            Reply::Deferred => WorkerReply::Deferred,
            other => WorkerReply::Data {
                data: reply_to_bytes(other),
                pending_fd,
            },
        }
    };

    reply_tx.send(worker_reply);
}

/// Find clients in ev.clients that were just created by a handler
/// (handle_new_process / handle_new_thread). Extract them, create
/// phantoms, and spawn worker threads.
fn spawn_new_clients(state: &mut BrokerState, skip_fd: RawFd) {
    // Find non-phantom clients that the broker doesn't have workers for.
    // New clients from handlers are created with is_phantom=false.
    let new_fds: Vec<RawFd> = state.ev.clients.iter()
        .filter(|(fd, c)| **fd != skip_fd && !c.is_phantom)
        .map(|(&fd, _)| fd)
        .collect();

    for new_fd in new_fds {
        // Extract the real client
        let real_client = match state.ev.clients.remove(&new_fd) {
            Some(c) => c,
            None => continue,
        };

        // Create phantom in its place
        let phantom = Client::phantom_from(&real_client);
        state.ev.clients.insert(new_fd, phantom);

        // Worker gets the original msg_fd — sole reader for SCM_RIGHTS.
        let worker_client = real_client;
        // msg_fd stays as-is from the real client

        // Spawn worker — handle failure gracefully instead of panicking.
        let broker_tx = state.broker_tx.clone();
        state.worker_count += 1;
        let worker_num = state.worker_count;
        let active_counter = state.active_workers.clone();
        active_counter.fetch_add(1, Ordering::Relaxed);
        match std::thread::Builder::new()
            .name(format!("w-{worker_num}"))
            .spawn(move || {
                crate::worker::worker_main(worker_client, broker_tx);
                active_counter.fetch_sub(1, Ordering::Relaxed);
            }) {
            Ok(_) => {}
            Err(_e) => {
                state.active_workers.fetch_sub(1, Ordering::Relaxed);
                state.ev.clients.remove(&new_fd);
            }
        }

    }
}

// ---- Disconnect: cleanup ----

fn handle_disconnect(state: &mut BrokerState, client_fd: RawFd, pid: u32, tid: u32) {
    // Guard against stale disconnect from fd reuse.
    // When a worker drops its Client (closing the fd), the kernel can reuse
    // that fd number for a new client created by new_process/new_thread.
    // If the stale Disconnect arrives after the fd was reused, we'd nuke
    // the new client's phantom. Check pid to detect this.
    if let Some(phantom) = state.ev.clients.get(&client_fd) {
        if phantom.process_id != 0 && phantom.process_id != pid {
            // The old phantom was already dropped when new_process/new_thread
            // replaced it in ev.clients. We still need to clean up the old
            // process's thread state so exit events fire correctly.
            state.ev.cleanup_stale_thread(pid, tid, client_fd);
            return;
        }
    } else {
        // No phantom at this fd — already cleaned up (e.g., by replacement drop)
        state.ev.cleanup_stale_thread(pid, tid, client_fd);
        return;
    }


    // Update phantom client's pid/tid so disconnect_client can find process state
    if let Some(phantom) = state.ev.clients.get_mut(&client_fd) {
        phantom.process_id = pid;
        phantom.thread_id = tid;
    }

    // Reuse EventLoop's disconnect_client for all cleanup:
    // signal exit events, process death detection, shutdown check
    state.ev.disconnect_client(client_fd);
}

// ---- Helpers ----

/// Serialize a Reply to bytes for sending to the worker.
fn reply_to_bytes(reply: Reply) -> Vec<u8> {
    match reply {
        Reply::Fixed { buf, len } => buf[..len].to_vec(),
        Reply::Vararg(data) => data,
        Reply::Deferred => {
            // Shouldn't reach here (handled separately), but be safe
            vec![0u8; 64]
        }
    }
}

