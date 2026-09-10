#!/bin/sh
# Read-only helpers for studying the legacy CNC branch during reimplementation.

set -eu

BASE_REF=${CNC_BASE_REF:-master}
REFERENCE_REF=${CNC_REFERENCE_REF:-develop}

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

git rev-parse --verify --quiet "${BASE_REF}^{commit}" >/dev/null || {
    echo "Unknown base ref: ${BASE_REF}" >&2
    exit 2
}
git rev-parse --verify --quiet "${REFERENCE_REF}^{commit}" >/dev/null || {
    echo "Unknown reference ref: ${REFERENCE_REF}" >&2
    exit 2
}

merge_base=$(git merge-base "$BASE_REF" "$REFERENCE_REF")

usage()
{
    echo "Usage: $0 COMMAND [PATH ...]"
    echo
    echo "Commands:"
    echo "  summary                 Show refs, merge base, and divergence"
    echo "  feature-stat            Summarize legacy changes from the merge base"
    echo "  feature-files [PATH]    List legacy-touched files"
    echo "  feature-diff [PATH]     Diff merge base to the legacy branch"
    echo "  current-diff [PATH]     Diff the current checkout to legacy state"
    echo "  log [PATH]              Show legacy-side commits after the merge base"
    echo "  show PATH               Print one file from the legacy branch"
}

command=${1:-summary}
if [ "$#" -gt 0 ]; then
    shift
fi

case "$command" in
    summary)
        echo "checkout:  $(git branch --show-current) @ $(git rev-parse --short HEAD)"
        echo "base:      ${BASE_REF} @ $(git rev-parse --short "$BASE_REF")"
        echo "reference: ${REFERENCE_REF} @ $(git rev-parse --short "$REFERENCE_REF")"
        echo "merge-base: $(git rev-parse --short "$merge_base")"
        set -- $(git rev-list --left-right --count \
            "${BASE_REF}...${REFERENCE_REF}")
        echo "divergence: ${BASE_REF} +$1, ${REFERENCE_REF} +$2"
        ;;
    feature-stat)
        git diff --stat "$merge_base" "$REFERENCE_REF" -- "$@"
        ;;
    feature-files)
        git diff --name-status "$merge_base" "$REFERENCE_REF" -- "$@"
        ;;
    feature-diff)
        git diff "$merge_base" "$REFERENCE_REF" -- "$@"
        ;;
    current-diff)
        git diff HEAD "$REFERENCE_REF" -- "$@"
        ;;
    log)
        git log --oneline --decorate "${merge_base}..${REFERENCE_REF}" -- "$@"
        ;;
    show)
        if [ "$#" -ne 1 ]; then
            usage >&2
            exit 2
        fi
        git show "${REFERENCE_REF}:$1"
        ;;
    help|-h|--help)
        usage
        ;;
    *)
        echo "Unknown command: ${command}" >&2
        usage >&2
        exit 2
        ;;
esac
