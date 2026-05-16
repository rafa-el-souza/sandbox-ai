// Command dispatch is the sandbox-ai runtime dispatcher.
//
// Scaffold status (Milestone 1): this is an argv-echo STUB. It parses the op
// from os.Args[1], prints a one-line summary of what it *would* dispatch, and
// exits 0. Milestone 3 replaces this with the real implementation: static
// op-table lookup, structured journald logging, target-argv construction from
// the shared JSON fixture, and process replacement via unix.Exec with
// EACCES/EIO/ENOENT error translation.
//
// Stdlib-only. No build tags. Keep minimal.
package main

import (
	"fmt"
	"os"
	"strings"
)

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: dispatch <op> [args...]")
		os.Exit(2)
	}

	op := os.Args[1]
	rest := os.Args[2:]

	fmt.Printf("would dispatch %s with args %s\n", op, strings.Join(rest, " "))
	os.Exit(0)
}
