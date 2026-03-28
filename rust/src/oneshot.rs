// Oneshot channel for broker request/reply communication.
// Built on std::sync::mpsc — zero external dependencies.

use std::sync::mpsc;

pub struct Sender<T> {
    tx: mpsc::Sender<T>,
}

pub struct Receiver<T> {
    rx: mpsc::Receiver<T>,
}

pub fn channel<T>() -> (Sender<T>, Receiver<T>) {
    let (tx, rx) = mpsc::channel();
    (Sender { tx }, Receiver { rx })
}

impl<T> Sender<T> {
    pub fn send(self, val: T) {
        let _ = self.tx.send(val);
    }
}

impl<T> Receiver<T> {
    pub fn try_recv(self) -> Option<T> {
        self.rx.recv().ok()
    }
}
