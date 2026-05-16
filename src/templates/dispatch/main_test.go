// Table-driven parity + confinement tests for the dispatcher.
//
// These run via `go test ./...` inside the pinned golang:1.23-alpine image as
// the FIRST step of the offline compile recipe (spec "Offline Reproducible
// Compile Recipe" / "Target Argv Construction Per Op" C-e). There is no host
// Go toolchain in the standard `make test`/`make coverage` gate, so a
// Python<->Go target-argv drift is caught deterministically at dispatcher
// compile time — the only place a Go toolchain exists — and a drifted binary
// is never produced or installed.
//
// The expected target-argv strings come from the ONE shared fixture
// src/templates/dispatch/fixtures/target_argv_cases.json, the same file the
// Python unit tests consume. No expected string is duplicated here.
package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

type fixtureCase struct {
	Op                 string   `json:"op"`
	Args               []string `json:"args"`
	ExpectedTargetArgv []string `json:"expected_target_argv"`
}

func loadFixture(t *testing.T) []fixtureCase {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("fixtures", "target_argv_cases.json"))
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}
	var cases []fixtureCase
	if err := json.Unmarshal(raw, &cases); err != nil {
		t.Fatalf("parse fixture: %v", err)
	}
	if len(cases) == 0 {
		t.Fatal("fixture is empty")
	}
	return cases
}

// TestTargetArgvFixtureParity is the load-bearing Python<->Go lockstep: every
// fixture row's wire args must produce expected_target_argv byte-for-byte.
func TestTargetArgvFixtureParity(t *testing.T) {
	for _, c := range loadFixture(t) {
		c := c
		name := c.Op + "-" + strings.Join(c.Args, "_")
		t.Run(name, func(t *testing.T) {
			got, _, err := buildTargetArgv(c.Op, c.Args)
			if err != nil {
				t.Fatalf("buildTargetArgv(%q, %v) errored: %v", c.Op, c.Args, err)
			}
			if !reflect.DeepEqual(got, c.ExpectedTargetArgv) {
				t.Fatalf("argv mismatch for op=%s args=%v\n got: %#v\nwant: %#v",
					c.Op, c.Args, got, c.ExpectedTargetArgv)
			}
		})
	}
}

// TestFixtureCoversAllTenOps asserts the fixture exercises every op (Q6 3.3b:
// keyed on the wire form so compose is a pure function of its inputs too).
func TestFixtureCoversAllTenOps(t *testing.T) {
	seen := map[string]bool{}
	for _, c := range loadFixture(t) {
		seen[c.Op] = true
	}
	for _, op := range validOps {
		if !seen[op] {
			t.Errorf("fixture does not cover op %q", op)
		}
	}
}

func TestUnknownOpRejected(t *testing.T) {
	tmp, err := os.CreateTemp(t.TempDir(), "stderr")
	if err != nil {
		t.Fatal(err)
	}
	code := run([]string{"dispatch", "hypothetical-not-a-real-op"}, tmp)
	if code != 2 {
		t.Fatalf("expected exit 2 for unknown op, got %d", code)
	}
	if _, err := tmp.Seek(0, 0); err != nil {
		t.Fatal(err)
	}
	buf, _ := os.ReadFile(tmp.Name())
	s := string(buf)
	if !strings.Contains(s, "unknown op: hypothetical-not-a-real-op") {
		t.Fatalf("stderr missing 'unknown op' line: %q", s)
	}
	if !strings.Contains(s, "valid ops:") {
		t.Fatalf("stderr missing valid-ops list: %q", s)
	}
}

func TestUnknownOpPlusCheckStillRejected(t *testing.T) {
	// Spec scenario "Unknown op + --check is rejected": op-name validation
	// fires BEFORE the --check predicate. --check does not whitewash.
	tmp, _ := os.CreateTemp(t.TempDir(), "stderr")
	code := run([]string{"dispatch", "bogus-op", "--check"}, tmp)
	if code != 2 {
		t.Fatalf("expected exit 2, got %d", code)
	}
	buf, _ := os.ReadFile(tmp.Name())
	if !strings.Contains(string(buf), "unknown op: bogus-op") {
		t.Fatalf("expected 'unknown op: bogus-op', got %q", string(buf))
	}
}

func TestComposeWireFlagParsing(t *testing.T) {
	base := []string{
		"myinst",
		"--project", "u-myinst",
		"--env-file", "/home/op/.sandbox-ai/instances/myinst/.sandbox.env",
		"--compose-file", "/home/op/.sandbox-ai/instances/myinst/docker/compose.yml",
	}
	t.Run("unknown flag rejected", func(t *testing.T) {
		_, _, err := buildComposeArgv("compose-up", append(append([]string{}, base...), "--runtime", "evil"))
		if err == nil || !strings.Contains(err.Error(), "unrecognized flag") {
			t.Fatalf("expected unrecognized-flag error, got %v", err)
		}
	})
	t.Run("volumes only for compose-down", func(t *testing.T) {
		_, _, err := buildComposeArgv("compose-up", append(append([]string{}, base...), "--volumes"))
		if err == nil || !strings.Contains(err.Error(), "only valid for compose-down") {
			t.Fatalf("expected volumes-rejection error, got %v", err)
		}
	})
	t.Run("project not ending -inst rejected", func(t *testing.T) {
		bad := []string{
			"myinst",
			"--project", "totally-unrelated",
			"--env-file", "/home/op/.sandbox-ai/instances/myinst/.sandbox.env",
			"--compose-file", "/home/op/.sandbox-ai/instances/myinst/docker/compose.yml",
		}
		_, _, err := buildComposeArgv("compose-up", bad)
		if err == nil || !strings.Contains(err.Error(), "must end with -myinst") {
			t.Fatalf("expected project-suffix error, got %v", err)
		}
	})
}

// confinement scenarios — these create real on-disk trees / symlinks so the
// lstat pass is genuinely exercised.

func TestConfinementRejectsOutsideEnvelope(t *testing.T) {
	// Spec scenario "dispatcher rejects a compose-file outside the instance
	// tree": /tmp/evil.yml is not under …/instances/myinst/.
	err := confinePathOperand("/tmp/evil.yml", "myinst")
	if err == nil || !strings.Contains(err.Error(), "outside the instances/myinst/ envelope") {
		t.Fatalf("expected envelope rejection, got %v", err)
	}
}

func TestConfinementRejectsDotDot(t *testing.T) {
	// Spec scenario "dispatcher rejects a compose path operand containing ..".
	err := confinePathOperand("/home/op/.sandbox-ai/instances/myinst/../other/compose.yml", "myinst")
	if err == nil || !strings.Contains(err.Error(), "empty/./.. component") {
		t.Fatalf("expected .. rejection, got %v", err)
	}
}

func TestConfinementRejectsRelative(t *testing.T) {
	err := confinePathOperand("instances/myinst/docker/compose.yml", "myinst")
	if err == nil || !strings.Contains(err.Error(), "must be absolute") {
		t.Fatalf("expected absolute-path rejection, got %v", err)
	}
}

func TestConfinementAcceptsInEnvelopeRealTree(t *testing.T) {
	// Spec scenario "dispatcher accepts in-envelope compose path operands".
	root := t.TempDir()
	instDir := filepath.Join(root, "instances", "myinst", "docker")
	if err := os.MkdirAll(instDir, 0o755); err != nil {
		t.Fatal(err)
	}
	composeYml := filepath.Join(instDir, "compose.yml")
	if err := os.WriteFile(composeYml, []byte("services: {}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	envFile := filepath.Join(root, "instances", "myinst", ".sandbox.env")
	if err := os.WriteFile(envFile, []byte(""), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := confinePathOperand(composeYml, "myinst"); err != nil {
		t.Fatalf("expected in-envelope compose.yml to pass, got %v", err)
	}
	if err := confinePathOperand(envFile, "myinst"); err != nil {
		t.Fatalf("expected in-envelope .sandbox.env to pass, got %v", err)
	}
}

func TestConfinementRejectsSymlinkedComponentInTree(t *testing.T) {
	// Spec scenario "dispatcher rejects a symlinked component inside the
	// instance tree": …/instances/myinst/docker is a symlink.
	root := t.TempDir()
	instMyinst := filepath.Join(root, "instances", "myinst")
	if err := os.MkdirAll(instMyinst, 0o755); err != nil {
		t.Fatal(err)
	}
	evil := filepath.Join(root, "evil")
	if err := os.MkdirAll(evil, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(evil, "compose.yml"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	dockerLink := filepath.Join(instMyinst, "docker")
	if err := os.Symlink(evil, dockerLink); err != nil {
		t.Fatal(err)
	}
	operand := filepath.Join(dockerLink, "compose.yml")
	err := confinePathOperand(operand, "myinst")
	if err == nil || !strings.Contains(err.Error(), "is a symbolic link") {
		t.Fatalf("expected symlinked-component rejection, got %v", err)
	}
}

func TestConfinementAcceptsSymlinkAboveInstancesBoundary(t *testing.T) {
	// Spec scenario "dispatcher does not symlink-check operator-home
	// ancestors": every component from instances/<inst> downward is a real
	// dir/file, but an ancestor ABOVE `instances` is a symlink. The check
	// must pass (ancestors above the boundary are intentionally NOT
	// symlink-checked — namespace stability).
	root := t.TempDir()
	realHome := filepath.Join(root, "var", "home", "op")
	if err := os.MkdirAll(filepath.Join(realHome, "instances", "myinst", "docker"), 0o755); err != nil {
		t.Fatal(err)
	}
	composeYml := filepath.Join(realHome, "instances", "myinst", "docker", "compose.yml")
	if err := os.WriteFile(composeYml, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	// /home -> /var/home style ancestor symlink, ABOVE the instances boundary.
	homeLink := filepath.Join(root, "home")
	if err := os.Symlink(filepath.Join(root, "var", "home"), homeLink); err != nil {
		t.Fatal(err)
	}
	operand := filepath.Join(homeLink, "op", "instances", "myinst", "docker", "compose.yml")
	if err := confinePathOperand(operand, "myinst"); err != nil {
		t.Fatalf("expected ancestor-above-instances symlink to be ignored, got %v", err)
	}
}

func TestConfinementFailClosedOnLstatError(t *testing.T) {
	// An lstat error on an in-envelope component (e.g. a missing parent dir)
	// is a fail-closed reject with an actionable diagnostic.
	root := t.TempDir()
	if err := os.MkdirAll(filepath.Join(root, "instances", "myinst"), 0o755); err != nil {
		t.Fatal(err)
	}
	// docker/ never created -> lstat of …/myinst/docker fails (ENOENT).
	operand := filepath.Join(root, "instances", "myinst", "docker", "compose.yml")
	err := confinePathOperand(operand, "myinst")
	if err == nil || !strings.Contains(err.Error(), "cannot lstat component") {
		t.Fatalf("expected fail-closed lstat error, got %v", err)
	}
	if !strings.Contains(err.Error(), "sandbox doctor") {
		t.Fatalf("lstat error should point at 'sandbox doctor', got %v", err)
	}
}

func TestComposeArgvHardcodedVerb(t *testing.T) {
	// Spec scenario "dispatcher does not take the compose verb from the wire":
	// the verb is exactly the op-hardcoded value. Use a real in-envelope tree.
	root := t.TempDir()
	dockerDir := filepath.Join(root, "instances", "myinst", "docker")
	if err := os.MkdirAll(dockerDir, 0o755); err != nil {
		t.Fatal(err)
	}
	composeYml := filepath.Join(dockerDir, "compose.yml")
	envFile := filepath.Join(root, "instances", "myinst", ".sandbox.env")
	for _, p := range []string{composeYml, envFile} {
		if err := os.WriteFile(p, []byte("x"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	wire := []string{
		"myinst",
		"--project", "u-myinst",
		"--env-file", envFile,
		"--compose-file", composeYml,
	}
	argv, inst, err := buildComposeArgv("compose-up", wire)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if inst != "myinst" {
		t.Fatalf("expected instance token myinst, got %q", inst)
	}
	if !strings.HasSuffix(argv[2], " up -d --build --wait") {
		t.Fatalf("compose-up verb must be the hardcoded 'up -d --build --wait', got %q", argv[2])
	}
	// compose-down --volumes -> "down -v".
	dwire := append([]string{
		"myinst",
		"--project", "u-myinst",
		"--env-file", envFile,
		"--compose-file", composeYml,
	}, "--volumes")
	dargv, _, err := buildComposeArgv("compose-down", dwire)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.HasSuffix(dargv[2], " down -v") {
		t.Fatalf("compose-down --volumes verb must be 'down -v', got %q", dargv[2])
	}
}

func TestCheckLoneArgShortCircuits(t *testing.T) {
	// Spec scenario "--check flag short-circuits to exit 0 without side
	// effect" (canonical predicate: op + lone --check). journald is likely
	// unavailable in the test container; the silent fallback keeps exit 0.
	tmp, _ := os.CreateTemp(t.TempDir(), "stderr")
	code := run([]string{"dispatch", "compose-up", "--check"}, tmp)
	if code != 0 {
		t.Fatalf("expected exit 0 for lone --check, got %d", code)
	}
}

func TestCheckNonLonePositionIsPositional(t *testing.T) {
	// Spec scenario "--check in a non-lone position does NOT short-circuit":
	// dispatch helper-chown-files /srv/parent 0644 1000 1000 --check treats
	// --check as a regular positional (it becomes a file name). We assert it
	// is NOT consumed by the short-circuit by checking the constructed argv.
	argv, _, err := buildTargetArgv(
		"helper-chown-files",
		[]string{"/srv/parent", "0644", "1000", "1000", "--check"},
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(argv[2], "for f in --check;") {
		t.Fatalf("--check should be a positional file name, got %q", argv[2])
	}
}

func TestShQuoteMatchesPythonShlex(t *testing.T) {
	cases := map[string]string{
		"a.log":           "a.log",
		"":                "''",
		"name with space": "'name with space'",
		"it's":            `'it'"'"'s'`,
		"a/b-c_d.e":       "a/b-c_d.e",
	}
	for in, want := range cases {
		if got := shQuote(in); got != want {
			t.Errorf("shQuote(%q) = %q, want %q", in, got, want)
		}
	}
}
