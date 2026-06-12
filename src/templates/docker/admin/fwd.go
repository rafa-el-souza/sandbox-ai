// Copyright (c) 2026 zerotrust-ai. SPDX-License-Identifier: AGPL-3.0-or-later
// fwd is the admin container's only binary.
//
// Two argv modes:
//   - Zero args: PID-1 idle. Block until SIGTERM/SIGINT, then exit 0.
//   - One arg "<host:port>": byte-pipe forwarder. Dial TCP and io.Copy
//     bidirectionally between stdio and the connection. Exit when either
//     direction EOFs.
//
// Stdlib only; CGO_ENABLED=0-friendly. Built with `go build -ldflags="-s -w"`
// in a multi-stage Dockerfile (golang:1.23-alpine builder → FROM scratch).
package main

import (
	"io"
	"net"
	"os"
	"os/signal"
	"syscall"
)

func main() {
	if len(os.Args) < 2 {
		// Zero-args branch: PID-1 idle mode.
		//
		// DO NOT "simplify" this to `select {}`.
		//
		// A bare `select {}` with no cases blocks the only goroutine forever,
		// which Go's runtime deadlock detector recognizes as
		// `fatal error: all goroutines are asleep - deadlock!` and aborts the
		// process with exit 2 on first start.
		//
		// `signal.Notify` defeats the detector by spawning an internal
		// goroutine in the signal package, giving the runtime another
		// runnable. Bonus: a clean SIGTERM-driven exit on `docker compose
		// stop` instead of needing SIGKILL.
		sig := make(chan os.Signal, 1)
		signal.Notify(sig, syscall.SIGTERM, syscall.SIGINT)
		<-sig
		return
	}

	// One-arg branch: byte-pipe forwarder.
	conn, err := net.Dial("tcp", os.Args[1])
	if err != nil {
		os.Exit(1)
	}
	defer conn.Close()

	done := make(chan struct{}, 2)
	go func() {
		_, _ = io.Copy(conn, os.Stdin)
		done <- struct{}{}
	}()
	go func() {
		_, _ = io.Copy(os.Stdout, conn)
		done <- struct{}{}
	}()
	<-done
}
