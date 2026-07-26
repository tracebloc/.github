#!/bin/sh
# house-rules.sh — portable house-rule checker for shell (and, via custom
# rules, any text) source files.
#
# WHY THIS EXISTS
#   A measured 14% of the org's automated code-review findings are tagged
#   "triggered by a learned rule" — team rules we already agreed on, being
#   re-discovered one PR at a time by a reviewer instead of by CI. This script
#   encodes the grep-level subset of those rules so they fail (or, while a repo
#   is still clearing its backlog, *report*) in CI at commit time.
#
# DESIGN CONSTRAINTS
#   * POSIX sh + awk only. No jq, no yq, no python, no bash-isms. Runs on a
#     GitHub runner, a Mac, or a minimal container with equal results.
#   * Precision over recall. A checker that cries wolf gets switched off, so
#     every rule below is deliberately narrowed to the case where the finding
#     is almost certainly real. Known blind spots are documented, not hidden.
#   * Extensible per repo without editing this file (see CONFIG FILE below).
#
# HOW IT AVOIDS FALSE POSITIVES
#   Shell cannot be linted with a plain `grep`: the word `curl` appears in
#   comments, in heredoc bodies, inside quoted strings, as part of an image
#   name (`curlimages/curl`), and as an argument (`command -v curl`). So this
#   script contains a small shell lexer (in awk) that:
#     - strips comments, quote-aware, so `${var#frag}` and "http://x/#y" survive
#     - skips heredoc bodies entirely (`<<EOF` … `EOF`, incl. `<<-` and quoted tags)
#     - joins `\`-continuations into one logical line, but reports the ORIGINAL
#       line number (so `kubectl rollout status \` + `--timeout=120s` is one
#       command with a timeout, not a violation)
#     - descends into `$( … )` even inside double quotes, so
#       `x="$(curl … | awk …)"` is still seen as a curl in a pipeline
#     - only matches a tool name in *command position* — after a line start,
#       `|`, `&&`, `;`, `(`, `$(`, `--`, or a shell keyword — never as a bare
#       argument, a path segment, or part of a longer word
#     - resolves flag-holding variables. `readonly CURL_SECURE="--tlsv1.2"` used
#       as `curl -fsSL $CURL_SECURE "$url"` satisfies the TLS rule: the script
#       is scanned for assignments whose value looks like flags, and those are
#       substituted before a flag is declared missing. Without this, the single
#       most common *correct* hardening idiom in this org would be the checker's
#       most common false positive.
#
# RULES
#   curl-tls        curl invoked without --tlsv1.2 (or --tlsv1.3).
#   curl-timeout    curl invoked without --max-time/-m or --connect-timeout.
#   kubectl-timeout kubectl wait | rollout status | delete without --timeout
#                   (or the global --request-timeout). Scoped to the blocking
#                   subcommands only: `kubectl rollout status` with no timeout
#                   waits forever, `kubectl get pods` cannot hang.
#   helm-timeout    helm install/upgrade/rollback/test/uninstall that waits
#                   (--wait/--atomic) without --timeout.
#   pipefail        A script that pipes the output of a fallible command
#                   (curl, helm, kubectl, git, …) into another command while
#                   the shell has no `set -o pipefail`: the pipeline reports
#                   the exit status of its LAST stage, so `curl … | bash` of a
#                   404 page exits 0 and the script marches on believing it
#                   succeeded. This exact defect shipped in a real installer.
#
# DELIBERATE EXCLUSIONS (these are precision choices, not oversights)
#   * `pipefail` skips POSIX-sh scripts (`#!/bin/sh`, `#!/usr/bin/env sh`).
#     `set -o pipefail` is not in POSIX and dash rejects it, so demanding it
#     there would be wrong advice. Such scripts must check the status of the
#     producer explicitly instead — which is not something grep can verify.
#   * `pipefail` skips files that some other file in the repo `source`s. A
#     sourced library inherits the entrypoint's shell options at runtime, so
#     flagging every lib/*.sh would be a wall of red about nothing.
#   * `curl --version` / `--help` and `helm --help` are ignored.
#
# SUPPRESSING A FINDING
#   Put a pragma on the offending line or the line directly above it:
#       curl -fsSL "$url"            # house-rules: ignore
#       curl -fsSL "$url"            # house-rules: ignore=curl-tls,curl-timeout
#   Bare `ignore` silences every rule for that line; `ignore=a,b` only those.
#
# CONFIG FILE (optional, repo root, default `.house-rules.conf`)
#   One directive per line; `#` starts a comment.
#     exclude: scripts/vendor/*          # skip paths (shell glob, repeatable)
#     wrapper: spin_cmd                  # a function that execs its arguments,
#                                        # so tools after it are in command position
#     timeout-wrapper: guard             # a wrapper that imposes its own time
#                                        # bound, so the *-timeout rules stand down
#     risky:   mytool                    # add to the pipefail producer list
#     disable: curl-timeout              # turn a built-in rule off
#     rule:    no-print | *.py | ^[[:space:]]*print\( | use client_logger, not print()
#   A `rule:` line is `id | path-glob | POSIX ERE | message` and is how a repo
#   adds its own house rules — including for languages this script knows
#   nothing about — without touching the shared workflow.
#
# USAGE
#   house-rules.sh [options] [file ...]
#     --base REF        check only files changed since REF (git diff REF...HEAD)
#     --all             check every tracked shell file (default with no args)
#     --config FILE     config file path (default .house-rules.conf)
#     --exclude GLOB    skip matching paths (repeatable)
#     --github          emit GitHub Actions annotations + step-summary rows
#     --summary FILE    append a markdown table to FILE
#     --soft-fail       always exit 0; report findings without failing
#     -h, --help        this text
#   Exit: 0 clean (or --soft-fail), 1 findings, 2 usage/internal error.

set -eu

PROG=$(basename "$0")
CONFIG=".house-rules.conf"
MODE="auto"
BASE=""
GITHUB_MODE=0
SUMMARY_FILE=""
SOFT_FAIL=0
EXCLUDES=""
FILES=""

die() {
    echo "$PROG: $1" >&2
    exit 2
}

usage() {
    sed -n '2,/^set -eu$/p' "$0" | sed 's/^# \{0,1\}//; $d'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --base)
            [ $# -ge 2 ] || die "--base needs a ref"
            BASE="$2"
            MODE="diff"
            shift 2
            ;;
        --all)
            MODE="all"
            shift
            ;;
        --config)
            [ $# -ge 2 ] || die "--config needs a path"
            CONFIG="$2"
            shift 2
            ;;
        --exclude)
            [ $# -ge 2 ] || die "--exclude needs a glob"
            EXCLUDES="$EXCLUDES
$2"
            shift 2
            ;;
        --github)
            GITHUB_MODE=1
            shift
            ;;
        --summary)
            [ $# -ge 2 ] || die "--summary needs a path"
            SUMMARY_FILE="$2"
            shift 2
            ;;
        --soft-fail)
            SOFT_FAIL=1
            shift
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        --)
            shift
            break
            ;;
        -*)
            die "unknown option: $1 (try --help)"
            ;;
        *)
            break
            ;;
    esac
done

for arg in "$@"; do
    FILES="$FILES
$arg"
    MODE="explicit"
done

TMPDIR_HR=$(mktemp -d)
# shellcheck disable=SC2064  # expand TMPDIR_HR now, on purpose
trap "rm -rf '$TMPDIR_HR'" EXIT INT TERM

CAND="$TMPDIR_HR/candidates"
SHELLFILES="$TMPDIR_HR/shellfiles"
SOURCED="$TMPDIR_HR/sourced"
CUSTOM="$TMPDIR_HR/custom"
FINDINGS="$TMPDIR_HR/findings"
FLAGVARS="$TMPDIR_HR/flagvars"
AWKPROG="$TMPDIR_HR/hr.awk"
: >"$CAND"
: >"$SHELLFILES"
: >"$SOURCED"
: >"$CUSTOM"
: >"$FINDINGS"
: >"$FLAGVARS"

# ---------------------------------------------------------------- config file

CFG_WRAPPERS=""
CFG_RISKY=""
CFG_DISABLED=""
CFG_TWRAPPERS=""

if [ -f "$CONFIG" ]; then
    # POSIX read loop. `|| [ -n "$line" ]` so a final line without a trailing
    # newline is still processed.
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            '#'* | '') continue ;;
        esac
        key=${line%%:*}
        val=${line#*:}
        # trim leading/trailing blanks without bashisms
        key=$(printf '%s' "$key" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
        val=$(printf '%s' "$val" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
        case "$key" in
            exclude) EXCLUDES="$EXCLUDES
$val" ;;
            wrapper) CFG_WRAPPERS="$CFG_WRAPPERS $val" ;;
            timeout-wrapper) CFG_TWRAPPERS="$CFG_TWRAPPERS $val" ;;
            risky) CFG_RISKY="$CFG_RISKY $val" ;;
            disable) CFG_DISABLED="$CFG_DISABLED $val" ;;
            rule) printf '%s\n' "$val" >>"$CUSTOM" ;;
            *) echo "$PROG: warning: $CONFIG: unknown directive '$key'" >&2 ;;
        esac
    done <"$CONFIG"
fi

# ------------------------------------------------------------ candidate files

in_git_repo=0
if git rev-parse --git-dir >/dev/null 2>&1; then
    in_git_repo=1
fi

case "$MODE" in
    explicit)
        printf '%s\n' "$FILES" | sed '/^$/d' >"$CAND"
        ;;
    diff)
        [ "$in_git_repo" = 1 ] || die "--base needs a git repository"
        # Three-dot: files this branch changed, not files the base moved on.
        # `|| true` because a shallow clone can lack the merge base; fall back
        # to a full scan rather than silently checking nothing.
        if ! git diff --name-only --diff-filter=ACMR "$BASE...HEAD" >"$CAND" 2>/dev/null; then
            echo "$PROG: warning: cannot diff against '$BASE' — scanning all tracked files" >&2
            git ls-files >"$CAND"
        fi
        if [ ! -s "$CAND" ]; then
            echo "$PROG: no changed files in $BASE...HEAD"
        fi
        ;;
    *)
        if [ "$in_git_repo" = 1 ]; then
            git ls-files >"$CAND"
        else
            find . -type f -print | sed 's|^\./||' >"$CAND"
        fi
        ;;
esac

# Keep only existing shell files, minus exclusions. Detection is by extension
# or by shebang, so an extension-less `bin/deploy` is still checked.
while IFS= read -r f; do
    [ -n "$f" ] || continue
    [ -f "$f" ] || continue
    skip=0
    # shellcheck disable=SC2254  # glob patterns in $pat are intentional
    while IFS= read -r pat; do
        [ -n "$pat" ] || continue
        case "$f" in
            $pat) skip=1 ;;
        esac
    done <<EOF
$EXCLUDES
EOF
    [ "$skip" = 0 ] || continue

    case "$f" in
        *.sh | *.bash | *.ksh | *.zsh) ;;
        *.bats | *.ps1 | *.psm1) continue ;;
        *)
            head -n 1 "$f" 2>/dev/null | grep -Eq '^#![[:space:]]*[^[:space:]]*(/|[[:space:]])(ba|da|k|z|a)?sh([[:space:]]|$)' || continue
            ;;
    esac
    printf '%s\n' "$f" >>"$SHELLFILES"
done <"$CAND"

# Which files are sourced by some other file? Those inherit their caller's
# shell options, so the pipefail rule must not fire on them. Match on
# basename: real code sources through a variable ("${LIB_DIR}/common.sh").
if [ -s "$SHELLFILES" ]; then
    if [ "$in_git_repo" = 1 ]; then
        git grep -hE '^[[:space:]]*(\.|source)[[:space:]]+[^|;&]+' -- '*.sh' '*.bash' 2>/dev/null || true
    else
        grep -rhE '^[[:space:]]*(\.|source)[[:space:]]+[^|;&]+' . 2>/dev/null || true
    fi |
        sed 's|.*/||; s/["'"'"'].*//; s/[[:space:]].*//' |
        grep -E '\.(sh|bash|ksh|zsh)$' | sort -u >"$SOURCED" || true
fi

# Flag-holding variables. `readonly CURL_SECURE="--tlsv1.2"` in one file is used
# as `curl $CURL_SECURE …` in another, so the table has to be repo-wide, not
# per-file. Only assignments whose value contains a `-`-prefixed token are
# collected: this is a flag table, not a general-purpose shell interpreter.
if [ "$in_git_repo" = 1 ]; then
    git grep -hE "^[[:space:]]*(readonly|export|local|declare)?[[:space:]]*[A-Za-z_][A-Za-z0-9_]*=[\"']?-" \
        -- '*.sh' '*.bash' '*.ksh' '*.zsh' 2>/dev/null || true
else
    grep -rhE "^[[:space:]]*(readonly|export|local|declare)?[[:space:]]*[A-Za-z_][A-Za-z0-9_]*=[\"']?-" . 2>/dev/null || true
fi |
    sed -E "s/^[[:space:]]*(readonly|export|local|declare)?[[:space:]]*//; s/[\"']//g; s/=/\t/; s/[[:space:]]*(#.*)?$//" |
    grep -E "^[A-Za-z_][A-Za-z0-9_]*$(printf '\t')-" | sort -u >"$FLAGVARS" || true

# ------------------------------------------------------------------ awk engine

cat >"$AWKPROG" <<'AWK_PROGRAM'
# Shell-aware house-rule engine. One pass per file; findings for a file are
# flushed when the next file starts (ENDFILE is a GNU extension, so this keeps
# to POSIX awk).

function ltrim(s) { sub(/^[[:space:]]+/, "", s); return s }
function rtrim(s) { sub(/[[:space:]]+$/, "", s); return s }

function glob2ere(g,   r) {
    r = g
    gsub(/[.+^$(){}\[\]|]/, "\\\\&", r)
    gsub(/\*/, ".*", r)
    gsub(/\?/, ".", r)
    return "^" r "$"
}

# Is `tok` present in `s` in *command position*? Returns its 1-based index or 0.
function findtok(s, tok,   off, pos, sub_s, before, after, pre) {
    off = 0
    while (1) {
        sub_s = substr(s, off + 1)
        pos = index(sub_s, tok)
        if (pos == 0) return 0
        pos = off + pos
        before = (pos == 1) ? "" : substr(s, pos - 1, 1)
        after = substr(s, pos + length(tok), 1)
        # Word boundary: not glued to an identifier, a path, or a version.
        if (before !~ /[A-Za-z0-9_.\/:-]/ && (after == "" || after ~ /[[:space:]]/)) {
            pre = substr(s, 1, pos - 1)
            # After a command separator, optionally negated.
            if (pre ~ /(^|[|&;({]|\$\(|--)[[:space:]]*(![[:space:]]+)?$/) return pos
            # After a shell keyword, optionally negated.
            if (pre ~ /(^|[|&;({]|[[:space:]])(sudo|time|exec|nohup|if|then|else|elif|do|while|until)[[:space:]]+(![[:space:]]+)?$/) return pos
            # After a repo-declared wrapper function that execs its arguments.
            if (WRAPPERS != "" && pre ~ WRAPPER_RE) return pos
            # After a time bound (`timeout 30 curl …`). Without this the tool is
            # invisible to every rule, so `curl-tls` silently stopped applying
            # to the exact hardening pattern the config documents as supported.
            if (TIMEOUT_PREFIX_RE != "" && pre ~ TIMEOUT_PREFIX_RE) return pos
        }
        off = pos + length(tok) - 1
    }
}

# Append the values of any flag-holding variables the segment references, so a
# flag passed as `$CURL_SECURE` counts as present. Returns the widened text used
# ONLY for flag-presence tests — never for command-position detection.
function expand_flags(seg,   name, out, pos, after) {
    out = seg
    for (name in flagvar) {
        pos = index(seg, "$" name)
        if (pos == 0) pos = index(seg, "${" name)
        if (pos == 0) continue
        # Reject a prefix match: $CURL must not satisfy a $CURL_SECURE reference.
        after = substr(seg, pos + length(name) + (substr(seg, pos + 1, 1) == "{" ? 2 : 1), 1)
        if (after ~ /[A-Za-z0-9_]/) continue
        out = out " " flagvar[name]
    }
    return out
}

# Does an external time bound already wrap this command (`timeout 30 curl …`)?
function externally_bounded(seg) {
    return (TIMEOUT_WRAPPER_RE != "" && seg ~ TIMEOUT_WRAPPER_RE)
}

function report(f, ln, rule, msg) {
    if (DISABLED != "" && index(" " DISABLED " ", " " rule " ") > 0) return
    if (ln in suppress_all && suppress_all[ln] == 1) return
    if ((ln SUBSEP rule) in suppress_rule) return
    # One report per (file, line, rule). A `\`-continued command can hold two
    # offending invocations (`curl … \` `|| ! curl …`); both resolve to the
    # logical line, and two identical annotations on one line is just noise.
    if ((f SUBSEP ln SUBSEP rule) in seen) return
    seen[f SUBSEP ln SUBSEP rule] = 1
    printf "%s\t%d\t%s\t%s\n", f, ln, rule, msg
    nfind++
}

# ---- per-file setup / teardown -------------------------------------------

function flush_file() {
    if (prev_file == "") return
    # pipefail is a whole-file property, so its findings are held back until
    # the file has been read in full.
    if (!pf_skip && !pf_has && pf_n > 0) {
        for (i = 1; i <= pf_n; i++) {
            report(prev_file, pf_line[i], "pipefail", \
                pf_tool[i] " pipes into another command but the script never sets `set -o pipefail`" \
                " — the pipeline exits with the status of its LAST stage, so a failing " pf_tool[i] \
                " is silently reported as success. Add `set -o pipefail` (with `set -eu`) near the top.")
        }
    }
    pf_n = 0
}

FNR == 1 {
    flush_file()
    prev_file = FILENAME
    # `split("", arr)` is the portable way to empty an array; `delete arr` is a
    # gawk extension and this must run under BSD awk too.
    split("", suppress_all)
    split("", suppress_rule)
    in_hd = 0; hd_tag = ""; hd_strip = 0
    pend_raw = ""; pend_mask = ""; pend_ln = 0
    Q = 0; sp = 0
    pf_has = 0; pf_n = 0; pf_skip = 0
    # `set -o pipefail` cannot be required of a POSIX-sh script (dash rejects
    # it), and a sourced library inherits it from whoever sources it.
    shebang = $0
    if (shebang ~ /^#!/ && shebang !~ /(bash|ksh|zsh)/) pf_skip = 1
    base = FILENAME
    sub(/.*\//, "", base)
    if (base in sourced) pf_skip = 1
}

# ---- suppression pragmas -------------------------------------------------
# Collected from the raw text before any stripping: a pragma lives in a comment.
{
    if ($0 ~ /house-rules:[[:space:]]*ignore/) {
        line_txt = $0
        if (match(line_txt, /house-rules:[[:space:]]*ignore=[A-Za-z0-9_,.-]+/)) {
            spec = substr(line_txt, RSTART, RLENGTH)
            sub(/.*ignore=/, "", spec)
            n = split(spec, parts, ",")
            for (i = 1; i <= n; i++) {
                r = ltrim(rtrim(parts[i]))
                if (r == "") continue
                suppress_rule[FNR SUBSEP r] = 1
                suppress_rule[(FNR + 1) SUBSEP r] = 1
            }
        } else {
            suppress_all[FNR] = 1
            suppress_all[FNR + 1] = 1
        }
    }
}

# ---- custom (config-supplied) rules -------------------------------------
# Applied to the raw line of any file type, before shell parsing, so a repo can
# lint Python, YAML or Go with the same mechanism.
{
    for (ci = 1; ci <= CUSTOM_N; ci++) {
        if (FILENAME ~ custom_glob[ci] && $0 ~ custom_re[ci]) {
            report(FILENAME, FNR, custom_id[ci], custom_msg[ci])
        }
    }
}

# ---- heredoc bodies are data, not code ----------------------------------
{
    if (in_hd) {
        cmpline = $0
        if (hd_strip) sub(/^[\t]+/, "", cmpline)
        if (cmpline == hd_tag) { in_hd = 0; hd_tag = "" }
        next
    }
}

# ---- lex one physical line ----------------------------------------------
{
    if (!IS_SHELL[FILENAME]) next

    line = $0
    craw = ""      # comment-stripped text, quotes kept
    cmask = ""     # same length, quoted spans blanked to _
    cont = 0
    i = 1
    n = length(line)
    while (i <= n) {
        c = substr(line, i, 1)
        if (Q == 0) {
            if (c == "\\") {
                nc = substr(line, i + 1, 1)
                if (nc == "") { cont = 1; break }
                craw = craw c nc; cmask = cmask "__"; i += 2; continue
            }
            if (c == "$" && substr(line, i + 1, 1) == "(") {
                sp++; qstack[sp] = Q
                craw = craw "$("; cmask = cmask "$("; i += 2; continue
            }
            if (c == ")" && sp > 0) {
                Q = qstack[sp]; sp--
                craw = craw c; cmask = cmask c; i++; continue
            }
            if (c == "'") { Q = 1; craw = craw c; cmask = cmask "_"; i++; continue }
            if (c == "\"") { Q = 2; craw = craw c; cmask = cmask "_"; i++; continue }
            if (c == "#") {
                prev = (length(cmask) == 0) ? "" : substr(cmask, length(cmask), 1)
                if (prev == "" || prev ~ /[[:space:]]/ || prev ~ /[|&;(]/) break
                craw = craw c; cmask = cmask c; i++; continue
            }
            craw = craw c; cmask = cmask c; i++; continue
        }
        if (Q == 1) {
            if (c == "'") Q = 0
            craw = craw c; cmask = cmask "_"; i++; continue
        }
        # Q == 2: double quotes still expand $( … ), so descend into it.
        if (c == "\\") {
            nc = substr(line, i + 1, 1)
            if (nc == "") { cont = 1; break }
            craw = craw c nc; cmask = cmask "__"; i += 2; continue
        }
        if (c == "$" && substr(line, i + 1, 1) == "(") {
            sp++; qstack[sp] = Q; Q = 0
            craw = craw "$("; cmask = cmask "$("; i += 2; continue
        }
        if (c == "\"") { Q = 0; craw = craw c; cmask = cmask "_"; i++; continue }
        craw = craw c; cmask = cmask "_"; i++; continue
    }

    if (pend_ln == 0) pend_ln = FNR
    pend_raw = pend_raw craw
    pend_mask = pend_mask cmask
    if (cont) next

    lraw = pend_raw
    lmask = pend_mask
    lno = pend_ln
    pend_raw = ""; pend_mask = ""; pend_ln = 0

    # `set -euo pipefail`, `set -o pipefail`, `set -eo pipefail`, and the
    # split forms `set -eu -o pipefail` / `set -e -o pipefail` — `-o pipefail`
    # may sit in its own flag word after earlier clusters. Tested on lmask, not
    # lraw: reading the raw line let `echo "set -o pipefail"` inside a string
    # mark the whole file as pipefail-safe and suppress every real finding.
    if (lmask ~ /(^|[[:space:];])set[[:space:]]+(-[a-zA-Z]+[[:space:]]+)*-[a-zA-Z]*o[a-zA-Z]*[[:space:]]+pipefail/) pf_has = 1

    # A heredoc opened on this logical line swallows the following lines.
    if (match(lraw, /<<[-~]?[[:space:]]*(\\)?("[A-Za-z_][A-Za-z0-9_]*"|'[A-Za-z_][A-Za-z0-9_]*'|[A-Za-z_][A-Za-z0-9_]*)/)) {
        tag = substr(lraw, RSTART, RLENGTH)
        hd_strip = (tag ~ /^<<-/)
        sub(/^<<[-~]?[[:space:]]*(\\)?/, "", tag)
        gsub(/["'\\]/, "", tag)
        if (tag != "") { in_hd = 1; hd_tag = tag }
    }

    check_line(lraw, lmask, lno)
}

# Split a logical line on command separators and apply the rules per segment.
function check_line(raw, mask, lno,   L, i, c, nc, segstart, seglen, is_pipe, prevseg, prevpipe) {
    L = length(mask)
    segstart = 1
    prevseg = ""
    prevpipe = 0
    i = 1
    while (i <= L + 1) {
        is_pipe = 0
        seglen = 0
        if (i > L) {
            seglen = i - segstart
        } else {
            c = substr(mask, i, 1)
            nc = substr(mask, i + 1, 1)
            if (c == "|" && nc == "|") { seglen = i - segstart; skip = 2 }
            else if (c == "&" && nc == "&") { seglen = i - segstart; skip = 2 }
            else if (c == "|") { seglen = i - segstart; skip = 1; is_pipe = 1 }
            else if (c == ";" || c == "&") { seglen = i - segstart; skip = 1 }
            else { i++; continue }
        }
        seg_raw = substr(raw, segstart, seglen)
        seg_mask = substr(mask, segstart, seglen)
        check_segment(seg_raw, seg_mask, lno)
        # A pipe means: the segment that just ended is a producer whose exit
        # status the shell will discard unless pipefail is set.
        if (is_pipe && !pf_skip) note_pipeline(seg_raw, seg_mask, lno)
        if (i > L) break
        i += skip
        segstart = i
    }
}

function note_pipeline(seg_raw, seg_mask, lno,   nr, i, t) {
    nr = split(RISKY, riskyarr, " ")
    for (i = 1; i <= nr; i++) {
        t = riskyarr[i]
        if (t == "") continue
        if (findtok(seg_mask, t) > 0) {
            pf_n++
            pf_line[pf_n] = lno
            pf_tool[pf_n] = "`" t "`"
            return
        }
    }
}

function check_segment(seg, mask, lno,   sub_cmd, segx, bounded) {
    segx = expand_flags(seg)
    bounded = externally_bounded(mask)

    # ---- curl ----
    if (findtok(mask, "curl") > 0) {
        if (segx !~ /(^|[[:space:]])(--version|--help|-V)([[:space:]]|$)/) {
            if (segx !~ /--tlsv1\.[23]/)
                report(FILENAME, lno, "curl-tls", \
                    "`curl` without `--tlsv1.2`: the request may negotiate a downgraded TLS version. Add `--tlsv1.2` (house rule).")
            if (!bounded && segx !~ /(^|[[:space:]])(--max-time|--connect-timeout|-[a-zA-Z]*m)([[:space:]]|=|$)/)
                report(FILENAME, lno, "curl-timeout", \
                    "`curl` without a timeout: a hung endpoint blocks the script forever. Add `--connect-timeout <s>` and `--max-time <s>`.")
        }
    }

    # ---- kubectl (blocking subcommands only) ----
    if (findtok(mask, "kubectl") > 0) {
        sub_cmd = ""
        if (seg ~ /(^|[[:space:]])wait([[:space:]]|$)/) sub_cmd = "wait"
        else if (seg ~ /(^|[[:space:]])rollout[[:space:]]+status([[:space:]]|$)/) sub_cmd = "rollout status"
        else if (seg ~ /(^|[[:space:]])delete([[:space:]]|$)/) sub_cmd = "delete"
        if (sub_cmd != "" && !bounded &&
            segx !~ /(--timeout|--request-timeout)([[:space:]]|=)/ &&
            segx !~ /(^|[[:space:]])--help([[:space:]]|$)/) {
            report(FILENAME, lno, "kubectl-timeout", \
                "`kubectl " sub_cmd "` without `--timeout`: it blocks indefinitely when the resource never settles, so CI hangs instead of failing. Add `--timeout=<s>`.")
        }
    }

    # ---- helm (only when it actually waits) ----
    if (findtok(mask, "helm") > 0) {
        if (seg ~ /(^|[[:space:]])(install|upgrade|uninstall|rollback|test)([[:space:]]|$)/ &&
            segx ~ /(^|[[:space:]])(--wait|--atomic)([[:space:]]|$)/ &&
            !bounded &&
            segx !~ /--timeout([[:space:]]|=)/ &&
            segx !~ /(^|[[:space:]])--help([[:space:]]|$)/) {
            report(FILENAME, lno, "helm-timeout", \
                "`helm` waits (`--wait`/`--atomic`) without `--timeout`: it falls back to the default wait and gives no bound you control. Add `--timeout <duration>`.")
        }
    }
}

BEGIN {
    nfind = 0
    prev_file = ""
    # Producers whose failure a pipeline would swallow.
    RISKY = "curl wget git helm kubectl docker podman az aws gcloud gh cosign terraform ansible python python3 go npm yarn pnpm openssl tar unzip sha256sum shasum"
    if (EXTRA_RISKY != "") RISKY = RISKY " " EXTRA_RISKY
    if (WRAPPERS != "") {
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", WRAPPERS)
        wr = WRAPPERS
        gsub(/[[:space:]]+/, "|", wr)
        # End-anchored, like the shell-keyword check above it. Unanchored, any
        # earlier wrapper name anywhere in the preceding text marked later
        # tokens as command position (`spin_cmd a && echo b` flagged `b`).
        WRAPPER_RE = "(^|[|&;({]|[[:space:]])(" wr ")[[:space:]]+(![[:space:]]+)?$"
    }
    # `timeout 30 curl …` already bounds the call; so does a repo-declared
    # equivalent (config: `timeout-wrapper: guard`).
    TW = "timeout" (TIMEOUT_WRAPPERS != "" ? " " TIMEOUT_WRAPPERS : "")
    gsub(/^[[:space:]]+|[[:space:]]+$/, "", TW)
    twr = TW
    gsub(/[[:space:]]+/, "|", twr)
    # A duration may carry a unit (`30s`), be a variable (`"$DUR"`), or be
    # absent entirely for a repo-declared wrapper (`timeout-wrapper: guard`,
    # used as `guard curl …`). Options may precede it (`timeout -k 5 30 …`,
    # `timeout --foreground 30 …`). Requiring a bare digit rejected all of
    # those and still raised *-timeout findings on correctly bounded calls.
    # Everything between the wrapper and the command it wraps: flags
    # (`--foreground`), flag arguments and durations with or without a unit
    # (`-k 5 30`, `30s`), and variables (`"$DUR"`). Zero of them is valid too,
    # for a repo-declared wrapper used as `guard curl ...`.
    TW_ARG = "((-[^[:space:]]+|[0-9]+[smhd]?|\"?\\$[{]?[A-Za-z_][A-Za-z0-9_]*[}]?\"?)[[:space:]]+)*"
    TIMEOUT_WRAPPER_RE = "(^|[|&;({]|[[:space:]])(" twr ")[[:space:]]+" TW_ARG
    # Same shape, end-anchored: decides whether the token that FOLLOWS a timeout
    # wrapper is in command position (finding 1).
    TIMEOUT_PREFIX_RE = "(^|[|&;({]|[[:space:]])(" twr ")[[:space:]]+" TW_ARG "$"
    # File list (so the shell layer decides what is a shell file, once).
    while ((getline l < SHELLFILE_LIST) > 0) if (l != "") IS_SHELL[l] = 1
    close(SHELLFILE_LIST)
    while ((getline l < SOURCED_LIST) > 0) if (l != "") sourced[l] = 1
    close(SOURCED_LIST)
    while ((getline l < FLAGVAR_LIST) > 0) {
        if (l == "") continue
        nf_fv = split(l, fv, "\t")
        if (nf_fv >= 2 && fv[1] != "") flagvar[fv[1]] = fv[2]
    }
    close(FLAGVAR_LIST)
    CUSTOM_N = 0
    while ((getline l < CUSTOM_LIST) > 0) {
        if (l == "") continue
        # id | glob | ere | message
        nf_cf = split(l, cf, "|")
        if (nf_cf < 4) {
            print "house-rules: warning: bad rule line: " l > "/dev/stderr"
            continue
        }
        CUSTOM_N++
        custom_id[CUSTOM_N] = ltrim(rtrim(cf[1]))
        custom_glob[CUSTOM_N] = glob2ere(ltrim(rtrim(cf[2])))
        custom_re[CUSTOM_N] = ltrim(rtrim(cf[3]))
        # A message may itself contain "|", so re-join every field past the third.
        # `length(array)` is a gawk extension, hence the split() return value.
        msg = cf[4]
        for (mi = 5; mi <= nf_cf; mi++) msg = msg "|" cf[mi]
        custom_msg[CUSTOM_N] = ltrim(rtrim(msg))
    }
    close(CUSTOM_LIST)
}

END {
    flush_file()
}
AWK_PROGRAM

# ---------------------------------------------------------------------- run

if [ ! -s "$SHELLFILES" ] && [ ! -s "$CUSTOM" ]; then
    echo "$PROG: no shell files to check."
    exit 0
fi

# Custom rules may target non-shell files, so feed awk the shell files plus
# anything a custom rule glob could match.
TOCHECK="$TMPDIR_HR/tocheck"
cp "$SHELLFILES" "$TOCHECK"
if [ -s "$CUSTOM" ]; then
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        [ -f "$f" ] || continue
        grep -qxF "$f" "$TOCHECK" 2>/dev/null || printf '%s\n' "$f" >>"$TOCHECK"
    done <"$CAND"
fi

if [ ! -s "$TOCHECK" ]; then
    echo "$PROG: no files to check."
    exit 0
fi

# `xargs`-free: awk takes the file list directly. Filenames with spaces are
# handled because we pass them as separate arguments via the shell read loop.
set --
while IFS= read -r f; do
    [ -n "$f" ] || continue
    set -- "$@" "$f"
done <"$TOCHECK"

awk \
    -v SHELLFILE_LIST="$SHELLFILES" \
    -v SOURCED_LIST="$SOURCED" \
    -v CUSTOM_LIST="$CUSTOM" \
    -v FLAGVAR_LIST="$FLAGVARS" \
    -v WRAPPERS="$CFG_WRAPPERS" \
    -v TIMEOUT_WRAPPERS="$CFG_TWRAPPERS" \
    -v EXTRA_RISKY="$CFG_RISKY" \
    -v DISABLED="$CFG_DISABLED" \
    -f "$AWKPROG" "$@" >"$FINDINGS" || die "awk engine failed"

COUNT=$(wc -l <"$FINDINGS" | tr -d ' ')
NFILES=$(wc -l <"$TOCHECK" | tr -d ' ')

if [ "$COUNT" = "0" ]; then
    echo "house-rules: no findings across $NFILES file(s)."
    if [ -n "$SUMMARY_FILE" ]; then
        {
            echo "### House rules"
            echo ""
            echo "No findings across $NFILES file(s)."
            echo ""
        } >>"$SUMMARY_FILE"
    fi
    exit 0
fi

# ------------------------------------------------------------------- report

echo "house-rules: $COUNT finding(s) across $NFILES file(s) checked:"
echo ""
while IFS="$(printf '\t')" read -r f ln rule msg; do
    printf '  %s:%s: [%s] %s\n' "$f" "$ln" "$rule" "$msg"
    if [ "$GITHUB_MODE" = 1 ]; then
        if [ "$SOFT_FAIL" = 1 ]; then lvl="warning"; else lvl="error"; fi
        # Annotation messages must not contain raw newlines.
        printf '::%s file=%s,line=%s,title=house-rules: %s::%s\n' \
            "$lvl" "$f" "$ln" "$rule" "$msg"
    fi
done <"$FINDINGS"
echo ""

if [ -n "$SUMMARY_FILE" ]; then
    {
        echo "### House rules"
        echo ""
        echo "**$COUNT finding(s)** across $NFILES file(s) checked."
        echo ""
        echo "| File | Line | Rule | Finding |"
        echo "|---|---:|---|---|"
        while IFS="$(printf '\t')" read -r f ln rule msg; do
            # Escape the markdown table delimiter.
            esc=$(printf '%s' "$msg" | sed 's/|/\\|/g')
            # shellcheck disable=SC2016  # backticks are markdown code spans, not substitution
            printf '| `%s` | %s | `%s` | %s |\n' "$f" "$ln" "$rule" "$esc"
        done <"$FINDINGS"
        echo ""
        echo "Silence one line with a pragma: \`# house-rules: ignore=<rule-id>\`."
        echo ""
    } >>"$SUMMARY_FILE"
fi

# Per-rule tally, so a repo can see which rule dominates its backlog.
echo "Findings by rule:"
cut -f3 "$FINDINGS" | sort | uniq -c | sort -rn | sed 's/^/  /'
echo ""

if [ "$SOFT_FAIL" = 1 ]; then
    echo "house-rules: soft-fail is on — reporting only, not failing this job."
    exit 0
fi
exit 1
