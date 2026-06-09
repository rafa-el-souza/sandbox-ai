// Command dispatch is the sandbox-ai runtime dispatcher.
//
// It is the *executor* half of OpenSpec change runtime-dispatcher (C-001): a
// single statically-linked, root-owned binary at the reserved path
// /usr/local/libexec/sandbox-ai/dispatch. The orchestrator narrows its
// privilege grant from "arbitrary bash as the sandbox user" to a fixed
// enumeration of typed ops crossed via:
//
//	sudo machinectl shell <user>@.host /bin/bash -c \
//	    "/usr/local/libexec/sandbox-ai/dispatch <op> <args...>"
//
// Responsibilities (design D4):
//   - parse argv[1] as the op; reject any op outside the static 11-op table;
//   - honor the `<op> --check` lone-arg short-circuit (sister-change L3a probe);
//   - write one structured journald entry before the spawn;
//   - construct the per-op target argv (deterministic ops: pure function of
//     typed args; compose ops: Q6 named-flag wire form with an op-hardcoded
//     verb plus the PURE LEXICAL half of the bounded structural confinement —
//     the construction path does zero filesystem I/O, so it is Python↔Go
//     byte-parity-asserted by the shared fixture);
//   - for compose ops, run the RUNTIME scoped-symlink confinement guard
//     (enforceComposeSymlinkSafety: the per-component lstat pass) on the
//     dispatch path, after the argv is built and before the spawn — relocated
//     off the pure construction path, not weakened;
//   - replace the process image via syscall.Exec, translating EACCES / EIO /
//     ENOENT into operator-meaningful hints.
//
// The Go binary trusts core.dispatch's Python validators (design D4); the only
// exception is the compose-op structural carve-out below. Stdlib-only, no
// build tags, no non-stdlib imports — the empty vendor / --network none
// offline-reproducible property (D3) depends on it. In particular process
// replacement uses stdlib syscall.Exec, NOT golang.org/x/sys/unix.
package main

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
)

// dispatchBinary is the reserved install path; surfaced in error hints.
const dispatchBinary = "/usr/local/libexec/sandbox-ai/dispatch"

// busyboxPinned is the digest-pinned busybox:musl ref. It is hardcoded here
// (and identically in core.hydration.IMAGE_REGISTRY["busybox_musl"].pinned and
// the shared fixture) so the helper-* target argv the Go binary reconstructs
// from the typed wire args is byte-identical to the Python builder's output.
const busyboxPinned = "busybox@sha256:3c6ae8008e2c2eedd141725c30b20d9c36b026eb796688f88205845ef17aa213"

// validOps is the static op table (spec "Typed Op Surface", 11 ops). Order is
// fixed so the "unknown op" diagnostic lists them deterministically.
var validOps = []string{
	"auth-probe",
	"compose-up",
	"compose-down",
	"compose-ps",
	"compose-ls",
	"docker-version",
	"docker-info",
	"docker-manifest-inspect",
	"helper-chown-files",
	"helper-mkdir-chown-dirs",
	"preflight",
}

// preflightInner is the byte-identical Go spelling of core.dispatch's
// _preflight_inner() — the ``;``-sequenced, per-query-attributed read-only
// health bundle backing ``sandbox start``'s privilege-boundary preflight
// (C-009 D6). It is necessarily a second spelling; the shared fixture
// (target_argv_cases.json, TestTargetArgvFixtureParity) pins it equal to the
// Python builder's output, so a drift fails ``go test`` -> the compile.
const preflightInner = "echo __PREFLIGHT_Q_auth-probe__; echo ok 2>&1; echo __PREFLIGHT_RC_auth-probe_$?__" +
	" ; echo __PREFLIGHT_Q_docker-version__; docker version --format '{{.Server.Version}}' 2>&1; echo __PREFLIGHT_RC_docker-version_$?__" +
	" ; echo __PREFLIGHT_Q_docker-info-security-options__; docker info --format '{{.SecurityOptions}}' 2>&1; echo __PREFLIGHT_RC_docker-info-security-options_$?__" +
	" ; echo __PREFLIGHT_Q_docker-info-runtimes__; docker info --format '{{json .Runtimes}}' 2>&1; echo __PREFLIGHT_RC_docker-info-runtimes_$?__" +
	" ; echo __PREFLIGHT_Q_compose-ls__; docker compose ls --format json --all 2>&1; echo __PREFLIGHT_RC_compose-ls_$?__"

// composeVerb is the op-hardcoded compose verb. It is NEVER taken from the
// wire; --volumes only flips compose-down to "down -v" (handled below). This
// mirrors core.dispatch._COMPOSE_VERB exactly.
var composeVerb = map[string]string{
	"compose-up":   "up -d --build --wait",
	"compose-down": "down",
	"compose-ps":   "ps --format json",
}

func isValidOp(op string) bool {
	for _, v := range validOps {
		if v == op {
			return true
		}
	}
	return false
}

func main() {
	os.Exit(dispatch(os.Args, os.Stdout, os.Stderr))
}

// genNonce returns a per-invocation 64-bit hex nonce. It is generated HERE —
// inside the trusted, root-owned dispatcher, AFTER sudo/polkit has authorized
// the bare `dispatch <op>` crossing — and NEVER passed in the authorized argv,
// so the rendered per-op Cmnd_Spec matches the bare command and never needs a
// wildcard to carry an exit sentinel (F-018). crypto/rand keeps the nonce
// unguessable by untrusted op output (a malicious image, the registry JSON a
// docker-manifest-inspect echoes, compose logs): that output cannot forge the
// trailer because it cannot read the dispatcher's prior stdout to learn the
// nonce (stdout is write-only for it; the agent's subuid is a separate
// pid/userns). A full sandbox-UID compromise is out of reach of any in-band
// scheme and is bounded by OS isolation + the immutable root-owned binary.
func genNonce() (string, error) {
	b := make([]byte, 8)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return hex.EncodeToString(b), nil
}

// wrapSentinel rewrites a `/bin/bash -c` inner so the inner's exit code is
// recovered past machinectl shell's exit-masking: it runs the inner in a
// SUBSHELL `( … )` and echoes the nonce-bound trailer carrying the subshell's
// `$?`. The subshell (not a brace group `{ … }`) is load-bearing: an `exit`
// inside the inner terminates only the subshell, so the trailer still runs —
// a brace group runs in the current shell and an inner `exit` would swallow the
// trailer entirely (the F-023 root cause that bit the orchestrator's Executor;
// the two wraps are kept in parity). This is the same recovery the Executor
// used to inject into the CROSSED payload — relocated here, post-authorization,
// so the sentinel never appears in the sudo/polkit-authorized command (F-018).
func wrapSentinel(inner, nonce string) string {
	return fmt.Sprintf("( %s ); echo __SANDBOX_EXIT_%s_$?", inner, nonce)
}

// dispatch is main's testable core. It generates the nonce, announces it on
// stdout (BEGIN line, before the op runs), then runs the op. On every path
// where run() RETURNS — usage/op-name/validation errors, the symlink-guard
// reject, --check, or an exec failure — it emits the nonce-bound EXIT trailer
// itself. On the success path run() replaces the process image with the
// sentinel-wrapped bash (and never returns), so that bash emits the trailer
// instead. Either way the trailer is emitted exactly once, bound to the BEGIN
// nonce the orchestrator captured. A crypto/rand failure emits NO BEGIN line,
// so the orchestrator fails closed (treats the crossing as failed) rather than
// trusting an unframed exit.
func dispatch(argv []string, out, errOut io.Writer) int {
	nonce, err := genNonce()
	if err != nil {
		fmt.Fprintf(errOut, "dispatch: cannot generate exit nonce: %v\n", err)
		return 70
	}
	fmt.Fprintf(out, "__SANDBOX_BEGIN_%s\n", nonce)
	code := run(argv, errOut, nonce)
	fmt.Fprintf(out, "__SANDBOX_EXIT_%s_%d\n", nonce, code)
	return code
}

// run is main's testable core. It returns the process exit code and writes
// diagnostics to errOut. On the success path it does not return — it replaces
// the process image via syscall.Exec.
func run(argv []string, errOut io.Writer, nonce string) int {
	if len(argv) < 2 {
		fmt.Fprintln(errOut, "usage: dispatch <op> [args...]")
		return 2
	}

	op := argv[1]

	// Op-name validation FIRST — before the --check predicate is consulted, so
	// an unknown op + --check is still rejected (spec scenario "Unknown op +
	// --check is rejected"). --check never whitewashes an invalid op.
	if !isValidOp(op) {
		fmt.Fprintf(errOut, "unknown op: %s\n", op)
		fmt.Fprintf(errOut, "valid ops: %s\n", strings.Join(validOps, ", "))
		return 2
	}

	rest := argv[2:]

	// --check lone-arg short-circuit. The canonical predicate (spec scenario
	// "--check flag short-circuits …" / "--check in a non-lone position does
	// NOT short-circuit") is EXACTLY: the op has exactly one trailing argument
	// equal to literal "--check". With argv == [dispatch, op, "--check"] that
	// is len(argv)==3 && argv[2]=="--check" (equivalently len(rest)==1 &&
	// rest[0]=="--check"). --check in any non-lone position is a normal
	// positional arg (no short-circuit). Op-name validation already ran above,
	// so an unknown op + --check was already rejected.
	if len(rest) == 1 && rest[0] == "--check" {
		journalLog(op, []string{"--check"}, nil, instanceForOp(op, nil), true)
		return 0
	}

	targetArgv, instance, err := buildTargetArgv(op, rest)
	if err != nil {
		fmt.Fprintln(errOut, err.Error())
		return 1
	}

	// Runtime symlink confinement (D4 carve-out, second half). The pure
	// target-argv construction above performs only the LEXICAL envelope checks
	// (Python↔Go byte-parity contract — fixture-asserted, zero disk I/O). The
	// filesystem `lstat` symlink pass is a RUNTIME guard on the dispatch path,
	// not part of the constructed argv: it runs here, after the argv is built
	// and before the process image is replaced, so a symlinked-in-tree operand
	// is rejected with no `os.execv`. Behaviour for a real on-disk tree is
	// identical to the previous (pre-split) implementation — the check moved
	// layers, it was not weakened.
	switch op {
	case "compose-up", "compose-down", "compose-ps":
		if err := enforceComposeSymlinkSafety(op, rest); err != nil {
			fmt.Fprintln(errOut, err.Error())
			return 1
		}
	}

	journalLog(op, rest, targetArgv, instance, false)

	// Relocate the exit sentinel into the dispatcher (post-authorization): the
	// command sudo/polkit authorized was the bare `dispatch <op>` (matching the
	// per-op Cmnd_Spec), so we wrap the bash -c inner HERE rather than letting
	// the orchestrator wrap the CROSSED payload (which no enumerated Cmnd_Spec
	// could match — F-018). Every buildTargetArgv arm returns bashC(...), so
	// targetArgv is [/bin/bash, -c, <inner>] and targetArgv[2] is the inner.
	targetArgv[2] = wrapSentinel(targetArgv[2], nonce)
	return execTarget(targetArgv, errOut)
}

// instanceForOp returns the instance token for compose ops (their first wire
// arg), empty otherwise. Used for the SANDBOX_AI_INSTANCE journald field.
func instanceForOp(op string, wire []string) string {
	switch op {
	case "compose-up", "compose-down", "compose-ps":
		if len(wire) > 0 {
			return wire[0]
		}
	}
	return ""
}

// ─── Target-argv construction ──────────────────────────────────────────────

func bashC(inner string) []string {
	return []string{"/bin/bash", "-c", inner}
}

// buildTargetArgv returns (targetArgv, instance, error). instance is the
// compose instance token (for journald), empty for the eight non-compose ops.
func buildTargetArgv(op string, args []string) ([]string, string, error) {
	switch op {
	case "auth-probe":
		return bashC("echo ok"), "", nil
	case "compose-ls":
		return bashC("docker compose ls --format json --all"), "", nil
	case "docker-version":
		return bashC("docker version --format '{{.Server.Version}}'"), "", nil
	case "preflight":
		return bashC(preflightInner), "", nil
	case "docker-info":
		if len(args) != 1 {
			return nil, "", fmt.Errorf("docker-info: expected exactly one preset arg")
		}
		var fmtStr string
		switch args[0] {
		case "security-options":
			fmtStr = "{{.SecurityOptions}}"
		case "runtimes":
			fmtStr = "{{json .Runtimes}}"
		default:
			return nil, "", fmt.Errorf("docker-info: unknown preset %q", args[0])
		}
		return bashC(fmt.Sprintf("docker info --format '%s'", fmtStr)), "", nil
	case "docker-manifest-inspect":
		if len(args) != 1 {
			return nil, "", fmt.Errorf("docker-manifest-inspect: expected exactly one image ref")
		}
		return bashC(fmt.Sprintf("docker manifest inspect %s", args[0])), "", nil
	case "helper-chown-files":
		return buildHelperChownFiles(args)
	case "helper-mkdir-chown-dirs":
		return buildHelperMkdirChownDirs(args)
	case "compose-up", "compose-down", "compose-ps":
		argv, inst, err := buildComposeArgv(op, args)
		return argv, inst, err
	}
	// Unreachable: op validity is checked in run() before this is called.
	return nil, "", fmt.Errorf("unknown op: %s", op)
}

func buildHelperChownFiles(args []string) ([]string, string, error) {
	if len(args) < 5 {
		return nil, "", fmt.Errorf("helper-chown-files: expected >=5 args")
	}
	parent, mode, uid, gid := args[0], args[1], args[2], args[3]
	files := args[4:]
	quoted := make([]string, len(files))
	for i, f := range files {
		quoted[i] = shQuote(f)
	}
	inner := fmt.Sprintf(
		"set -e; for f in %s; do "+
			`cp /p/"$f" /tmp/"$f" && `+
			`unlink /p/"$f" && `+
			`cp /tmp/"$f" /p/"$f" && `+
			`chmod %s /p/"$f" && `+
			`chown %s:%s /p/"$f"; `+
			"done",
		strings.Join(quoted, " "), mode, uid, gid,
	)
	return bashC(hardenedDockerRun(busyboxPinned, parent, inner)), "", nil
}

func buildHelperMkdirChownDirs(args []string) ([]string, string, error) {
	if len(args) < 4 {
		return nil, "", fmt.Errorf("helper-mkdir-chown-dirs: expected >=4 args")
	}
	parent, uid, gid := args[0], args[1], args[2]
	leaves := args[3:]
	quoted := make([]string, len(leaves))
	for i, d := range leaves {
		quoted[i] = shQuote(d)
	}
	inner := fmt.Sprintf(
		"set -e; for d in %s; do "+
			`mkdir -p /p/"$d" && chown %s:%s /p/"$d"; `+
			"done",
		strings.Join(quoted, " "), uid, gid,
	)
	return bashC(hardenedDockerRun(busyboxPinned, parent, inner)), "", nil
}

// hardenedDockerRun mirrors core.helper_container._hardened_docker_run
// byte-for-byte (the SPACE-separated --cap-drop ALL form, NOT --cap-drop=ALL).
func hardenedDockerRun(image, parent, innerSh string) string {
	return "docker run --rm " +
		"--runtime=runc " +
		"--network=none " +
		"--read-only " +
		"--tmpfs /tmp " +
		"--user 0:0 " +
		"--cap-drop ALL " +
		"--cap-add CHOWN " +
		"--cap-add DAC_OVERRIDE " +
		"--security-opt no-new-privileges:true " +
		fmt.Sprintf("-v %s:/p ", shQuote(parent)) +
		fmt.Sprintf("%s ", shQuote(image)) +
		fmt.Sprintf("sh -c %s", shQuote(innerSh))
}

// shQuote reproduces Python's shlex.quote byte-for-byte: empty -> a quote pair; a string
// of only the safe set [\w@%+=:,./-] -> unquoted; otherwise single-quote-wrap
// with embedded ' rendered as '"'"'.
var shSafeRe = regexp.MustCompile(`^[\w@%+=:,./-]+$`)

func shQuote(s string) string {
	if s == "" {
		return "''"
	}
	if shSafeRe.MatchString(s) {
		return s
	}
	return "'" + strings.ReplaceAll(s, "'", `'"'"'`) + "'"
}

// ─── Compose-op wire parsing + structural / symlink confinement (D4 carve) ──

var projectRe = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]*$`)

// composeWire is the parsed Q6 named-flag wire form.
type composeWire struct {
	inst         string
	project      string
	envFile      string
	composeFiles []string
	volumes      bool
}

// parseComposeWire parses the Q6 named-flag wire form. It is PURE — wire-flag
// lexing only, NO filesystem access — so it is shared between the pure
// target-argv construction path (buildComposeArgv, fixture-parity asserted)
// and the runtime symlink guard (enforceComposeSymlinkSafety) without dragging
// disk I/O into the construction path.
//
// Wire form: <inst> --project <P> --env-file <E> --compose-file <f1> [...] [--volumes]
func parseComposeWire(op string, wire []string) (composeWire, error) {
	var cw composeWire
	if len(wire) == 0 {
		return cw, fmt.Errorf("%s: missing <instance> in wire form", op)
	}
	cw.inst = wire[0]
	rest := wire[1:]

	var haveProject, haveEnv bool
	for i := 0; i < len(rest); {
		flag := rest[i]
		if flag == "--volumes" {
			if op != "compose-down" {
				return cw, fmt.Errorf("%s: --volumes is only valid for compose-down", op)
			}
			if cw.volumes {
				return cw, fmt.Errorf("%s: --volumes given more than once", op)
			}
			cw.volumes = true
			i++
			continue
		}
		if i+1 >= len(rest) {
			return cw, fmt.Errorf("%s: flag %q is missing its value", op, flag)
		}
		value := rest[i+1]
		switch flag {
		case "--project":
			if haveProject {
				return cw, fmt.Errorf("%s: --project given more than once", op)
			}
			cw.project, haveProject = value, true
		case "--env-file":
			if haveEnv {
				return cw, fmt.Errorf("%s: --env-file given more than once", op)
			}
			cw.envFile, haveEnv = value, true
		case "--compose-file":
			cw.composeFiles = append(cw.composeFiles, value)
		default:
			return cw, fmt.Errorf("%s: unrecognized flag %q", op, flag)
		}
		i += 2
	}

	if !haveProject {
		return cw, fmt.Errorf("%s: --project is required exactly once", op)
	}
	if !haveEnv {
		return cw, fmt.Errorf("%s: --env-file is required exactly once", op)
	}
	if len(cw.composeFiles) == 0 {
		return cw, fmt.Errorf("%s: at least one --compose-file is required", op)
	}

	// --project: charset + ends with -<inst> (spec "Compose Op Wire Expansion").
	// Pure lexical (regex + suffix) — part of target-argv construction.
	if !projectRe.MatchString(cw.project) {
		return cw, fmt.Errorf(
			"%s: --project %q must match ^[a-z0-9][a-z0-9_-]*$", op, cw.project)
	}
	if !strings.HasSuffix(cw.project, "-"+cw.inst) {
		return cw, fmt.Errorf(
			"%s: --project %q must end with -%s", op, cw.project, cw.inst)
	}
	return cw, nil
}

// buildComposeArgv parses the Q6 named-flag wire form, applies the PURE
// LEXICAL portion of the bounded structural confinement (spec "Compose Op
// Wire Expansion"), and assembles the target argv with an op-hardcoded verb.
//
// This function is part of the Python↔Go byte-parity contract asserted by the
// shared fixture (TestTargetArgvFixtureParity) and therefore performs ZERO
// filesystem access. The runtime `lstat` symlink pass is NOT performed here —
// it is a separate dispatch-path guard (enforceComposeSymlinkSafety, called
// from run()).
//
// Wire form: <inst> --project <P> --env-file <E> --compose-file <f1> [...] [--volumes]
func buildComposeArgv(op string, wire []string) ([]string, string, error) {
	cw, err := parseComposeWire(op, wire)
	if err != nil {
		return nil, "", err
	}

	// PURE LEXICAL confinement on every path operand (absolute, no
	// empty/./.. component, no NUL/newline, inside the instances/<inst>
	// envelope). NO filesystem access — this is part of the byte-parity
	// target-argv contract. The runtime `lstat` symlink pass is performed
	// separately by enforceComposeSymlinkSafety from run().
	for _, p := range cw.composeFiles {
		if err := confinePathOperand(p, cw.inst); err != nil {
			return nil, "", err
		}
	}
	if err := confinePathOperand(cw.envFile, cw.inst); err != nil {
		return nil, "", err
	}

	// Assemble. The verb is op-hardcoded and NEVER read from the wire.
	verb := composeVerb[op]
	if op == "compose-down" && cw.volumes {
		verb = "down -v"
	}
	fParts := make([]string, 0, len(cw.composeFiles))
	for _, f := range cw.composeFiles {
		fParts = append(fParts, "-f "+f)
	}
	filesStr := strings.Join(fParts, " ")
	envPrefix := fmt.Sprintf(
		"TERM=dumb NO_COLOR=1 BUILDKIT_PROGRESS=plain COMPOSE_PROJECT_NAME=%s", cw.project)

	var inner string
	if op == "compose-ps" {
		inner = fmt.Sprintf(
			"%s docker compose %s --env-file %s --ansi never %s",
			envPrefix, filesStr, cw.envFile, verb)
	} else {
		inner = fmt.Sprintf(
			"%s docker compose %s --ansi never --env-file %s %s",
			envPrefix, filesStr, cw.envFile, verb)
	}
	return bashC(inner), cw.inst, nil
}

// confinePathOperand applies the bounded D4 carve-out to a compose path
// operand. This is the PURE LEXICAL half — NO filename allowlist, NO path
// resolution, NO filesystem access (namespace-stable):
//
//  1. absolute; no empty / "." / ".." component; no NUL / newline byte;
//  2. contains the consecutive components `instances` then <inst> with at
//     least one further component below <inst>.
//
// These deterministic string checks are part of the target-argv construction
// contract (Python↔Go byte-parity, fixture-asserted via
// TestTargetArgvFixtureParity) and run with zero disk I/O. The filesystem
// `lstat` symlink pass is a SEPARATE runtime guard
// (enforceComposeSymlinkSafety) on the dispatch path — it is NOT performed
// here; see that function for the symlink semantics and the
// honestly-documented TOCTOU residual.
func confinePathOperand(p, inst string) error {
	_, err := composeEnvelopeBoundary(p, inst)
	return err
}

// composeEnvelopeBoundary runs the pure lexical envelope checks and, on
// success, returns the path components together with the index of the <inst>
// component (the instances/<inst> boundary). It performs NO filesystem access
// and is shared by confinePathOperand (pure construction) and
// enforceComposeSymlinkSafety (runtime guard) so the boundary is located
// identically in both layers.
func composeEnvelopeBoundary(p, inst string) (boundary composeBoundary, err error) {
	if strings.IndexByte(p, 0) >= 0 {
		return boundary, fmt.Errorf("compose path operand %q contains a NUL byte", p)
	}
	if strings.IndexByte(p, '\n') >= 0 {
		return boundary, fmt.Errorf("compose path operand %q contains a newline byte", p)
	}
	if !strings.HasPrefix(p, "/") {
		return boundary, fmt.Errorf("compose path operand %q must be absolute", p)
	}

	parts := strings.Split(p, "/") // leading "" from the root slash
	comps := parts[1:]
	for _, c := range comps {
		if c == "" || c == "." || c == ".." {
			return boundary, fmt.Errorf(
				"compose path operand %q has an empty/./.. component (outside the instances/%s envelope)",
				p, inst)
		}
	}

	// Locate the consecutive `instances` then <inst> components, with at least
	// one component below <inst>.
	instIdx := -1
	for idx := 0; idx+1 < len(comps); idx++ {
		if comps[idx] == "instances" && comps[idx+1] == inst {
			instIdx = idx + 1 // index of the <inst> component
			break
		}
	}
	if instIdx < 0 || instIdx+1 >= len(comps) {
		return boundary, fmt.Errorf(
			"compose path operand %q is outside the instances/%s/ envelope", p, inst)
	}
	return composeBoundary{comps: comps, instIdx: instIdx}, nil
}

// composeBoundary is the lexical-pass result handed to the runtime symlink
// guard: the split path components and the index of the <inst> component.
type composeBoundary struct {
	comps   []string
	instIdx int
}

// enforceComposeSymlinkSafety is the RUNTIME half of the D4 carve-out. For
// every `--compose-file` and the `--env-file` it lstat()s each path component
// FROM the instances/<inst> boundary DOWNWARD to and including the operand
// file and rejects any symlink; it is fail-closed on any lstat error with an
// actionable diagnostic. Components ABOVE the `instances` boundary
// (operator-home ancestors) are intentionally NOT symlink-checked — checking
// them would reintroduce the namespace-fragile false-reject behaviour that
// ruled out realpath (distro /home -> /var/home, systemd ProtectHome views).
//
// This is RELOCATED, not removed: the runtime behaviour for a real on-disk
// tree is identical to the previous (pre-split) implementation. It moved off
// the pure target-argv construction path (so the Python↔Go byte-parity
// fixture asserts construction with zero disk I/O) and onto the dispatch path
// in run(), where it runs after the argv is built and before os.execv.
//
// It is deliberately NOT TOCTOU-complete: it cannot close the race between
// this check and docker compose's later open() of -f <path>. The only actor
// able to win that race is one with write access to the operator-owned
// …/instances/<inst>/ tree — i.e. the operator, who already holds passwordless
// arbitrary command execution via the F-003-unclosable sudoers grant. The
// residual grants that actor no power they lack; tracked in deferred.md.
//
// It re-runs the pure lexical envelope check (cheap, no disk I/O) so it can be
// called from run() with only (op, wire) and remain correct even if invoked
// independently of buildComposeArgv.
func enforceComposeSymlinkSafety(op string, wire []string) error {
	cw, err := parseComposeWire(op, wire)
	if err != nil {
		return err
	}
	operands := make([]string, 0, len(cw.composeFiles)+1)
	operands = append(operands, cw.composeFiles...)
	operands = append(operands, cw.envFile)
	for _, p := range operands {
		b, err := composeEnvelopeBoundary(p, cw.inst)
		if err != nil {
			return err
		}
		// lstat each component from the instances/<inst> boundary downward,
		// including the operand file. Build the absolute prefix up to <inst>
		// first.
		prefix := "/" + strings.Join(b.comps[:b.instIdx+1], "/")
		if err := rejectSymlink(prefix, p, cw.inst); err != nil {
			return err
		}
		cur := prefix
		for _, c := range b.comps[b.instIdx+1:] {
			cur = filepath.Join(cur, c)
			if err := rejectSymlink(cur, p, cw.inst); err != nil {
				return err
			}
		}
	}
	return nil
}

func rejectSymlink(component, operand, inst string) error {
	fi, err := os.Lstat(component)
	if err != nil {
		// Fail-closed. An lstat error here (typically EACCES on the parent
		// directory) necessarily implies the real `docker compose -f <path>`
		// open would also fail — lstat needs a subset of the traversal docker
		// compose already requires — so this is fail-fast on an already-broken
		// setup, never a false reject of a working one.
		return fmt.Errorf(
			"compose path operand %q: cannot lstat component %q (%v); "+
				"the sandbox user likely lacks an ACL traverse grant on its "+
				"parent directory — run 'sandbox doctor'",
			operand, component, err)
	}
	if fi.Mode()&os.ModeSymlink != 0 {
		return fmt.Errorf(
			"compose path operand %q: component %q is a symbolic link "+
				"(rejected inside the instances/%s/ envelope)",
			operand, component, inst)
	}
	return nil
}

// ─── Structured journald logging ───────────────────────────────────────────

const journalSocket = "/run/systemd/journal/socket"

func truncate256(s string) string {
	if len(s) > 256 {
		return s[:256]
	}
	return s
}

// journalLog writes a single structured entry to systemd-journald via the
// native journal protocol over the unix datagram socket. Failure is silent
// (a stderr note only) — the op is never failed because journald is down.
func journalLog(op string, args, targetArgv []string, instance string, check bool) {
	argsSummary := truncate256(strings.Join(args, ","))
	targetSummary := truncate256(strings.Join(targetArgv, " "))
	message := fmt.Sprintf("dispatch %s %s", op, strings.Join(args, " "))

	fields := map[string]string{
		"MESSAGE":                        message,
		"PRIORITY":                       "6",
		"SANDBOX_AI_OP":                  op,
		"SANDBOX_AI_ARGS_SUMMARY":        argsSummary,
		"SANDBOX_AI_TARGET_ARGV_SUMMARY": targetSummary,
		"SANDBOX_AI_INSTANCE":            instance,
	}
	if check {
		fields["SANDBOX_AI_CHECK"] = "1"
	}

	payload := encodeJournalFields(fields)

	conn, err := net.Dial("unixgram", journalSocket)
	if err != nil {
		fmt.Fprintf(os.Stderr, "dispatch: journald unavailable (%v); continuing\n", err)
		return
	}
	defer conn.Close()
	if _, err := conn.Write(payload); err != nil {
		fmt.Fprintf(os.Stderr, "dispatch: journald write failed (%v); continuing\n", err)
	}
}

// encodeJournalFields serializes fields in the native journal export format:
// a field with no newline is "KEY=VALUE\n"; a field whose value contains a
// newline is "KEY\n<le64 length><raw value>\n". All our values are
// newline-free in practice, but the multiline form is implemented for
// faithfulness.
func encodeJournalFields(fields map[string]string) []byte {
	var b strings.Builder
	for k, v := range fields {
		if strings.ContainsRune(v, '\n') {
			b.WriteString(k)
			b.WriteByte('\n')
			var l [8]byte
			n := uint64(len(v))
			for i := 0; i < 8; i++ {
				l[i] = byte(n >> (8 * uint(i)))
			}
			b.Write(l[:])
			b.WriteString(v)
			b.WriteByte('\n')
		} else {
			b.WriteString(k)
			b.WriteByte('=')
			b.WriteString(v)
			b.WriteByte('\n')
		}
	}
	return []byte(b.String())
}

// ─── Process replacement + system-binary error translation ─────────────────

// translateExecError maps a failed syscall.Exec error into the spec-pinned
// (exit code, operator-meaningful hint) pair. It is a pure function extracted
// as a test seam: EIO is not reliably reproducible by a real exec without a
// device fault, so the EIO branch is exercised by feeding this helper
// syscall.EIO directly. execTarget calls it on the (only-returns-on-failure)
// path.
func translateExecError(err error, target string) (int, string) {
	switch {
	case errors.Is(err, syscall.EACCES):
		return 126, fmt.Sprintf(
			"process replacement refused by kernel (EACCES); likely IMA-appraise "+
				"or fapolicyd is enforcing on %s. Check system integrity tool "+
				"state via 'sandbox doctor'.\n", target)
	case errors.Is(err, syscall.EIO):
		return 127, fmt.Sprintf(
			"I/O error during process replacement (EIO); likely dm-verity reports "+
				"block-level corruption on %s's partition. Check dmesg for verity "+
				"events.\n", target)
	case errors.Is(err, syscall.ENOENT):
		return 127, fmt.Sprintf(
			"target binary not found at %s; the package providing it may be "+
				"uninstalled. Reinstall the package and re-run 'sudo sandbox "+
				"setup'.\n", target)
	default:
		return 1, fmt.Sprintf("dispatch: exec %s failed: %v\n", target, err)
	}
}

// execTarget replaces the process image with targetArgv via stdlib
// syscall.Exec (Linux). On EACCES / EIO / ENOENT it returns the spec-pinned
// exit code and writes the spec-asserted hint substrings to errOut. On any
// other error it returns 1.
func execTarget(targetArgv []string, errOut io.Writer) int {
	target := targetArgv[0]
	err := syscall.Exec(target, targetArgv, os.Environ())
	// syscall.Exec only returns on failure.
	code, hint := translateExecError(err, target)
	fmt.Fprint(errOut, hint)
	return code
}
