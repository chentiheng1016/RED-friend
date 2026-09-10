#!/bin/bash
# install-hooks.sh — symlink versioned hooks from scripts/hooks/ into .git/hooks/
#
# Run once per clone (from the main checkout, not a worktree):
#   ./scripts/install-hooks.sh
#
# Uses RELATIVE symlinks targeting the main repo's scripts/hooks/, not the
# worktree we happened to be in. That way the symlinks keep working after
# the worktree is removed, and the same hook fires for every worktree
# (which is actually the git common-dir behaviour we want).

set -euo pipefail

# git_common_dir is shared across all worktrees of a repo; show-toplevel
# of THE common dir gives us the main checkout's working tree.
HOOK_DIR="$(cd "$(git rev-parse --git-common-dir)" && pwd)/hooks"
# Nested dirname (NOT pipe to xargs) preserves paths containing whitespace
# — `xargs dirname` would word-split "/tmp/repo with space/.git" into three
# args before dirname ever ran.
MAIN_REPO_ROOT="$(dirname "$(dirname "$HOOK_DIR")")"
SOURCE_DIR="$MAIN_REPO_ROOT/scripts/hooks"

if [ ! -d "$SOURCE_DIR" ]; then
    echo "✗ scripts/hooks not found at $SOURCE_DIR"
    echo "  Run this from a checkout that has the new hooks merged in."
    exit 1
fi

mkdir -p "$HOOK_DIR"
installed=0
for hook in "$SOURCE_DIR"/*; do
    [ -f "$hook" ] || continue
    name="$(basename "$hook")"
    target="$HOOK_DIR/$name"
    chmod +x "$hook"

    # Relative symlink from .git/hooks/ → ../../scripts/hooks/<name>.
    # Survives worktree removal, since main repo's scripts/hooks/ is the
    # canonical source.
    rel_target="../../scripts/hooks/$name"

    if [ -L "$target" ] && [ "$(readlink "$target")" = "$rel_target" ]; then
        echo "✓ already installed: $name"
        continue
    fi
    # Refuse to clobber an existing hook — whether it's a regular file OR a
    # symlink pointing somewhere we don't own (user's own custom hook). Use
    # `-e || -L` because `-e` returns false for broken symlinks but `-L`
    # catches them.
    if [ -e "$target" ] || [ -L "$target" ]; then
        if [ -L "$target" ]; then
            echo "⚠️  $name is a symlink to $(readlink "$target") — leaving it alone"
        else
            echo "⚠️  $name already exists in $HOOK_DIR — leaving it alone"
        fi
        echo "   (move it aside, then re-run this installer)"
        continue
    fi

    ln -sf "$rel_target" "$target"
    installed=$((installed + 1))
    echo "✓ installed: $name → $rel_target"
done

echo
if [ "$installed" -gt 0 ]; then
    echo "Installed $installed hook(s). They run on the next git operation."
else
    echo "Nothing new to install."
fi
