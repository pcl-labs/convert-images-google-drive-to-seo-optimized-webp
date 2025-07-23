#!/bin/bash
# Fix pyenv PATH so shims are first, ensuring correct Python version
export PATH="$HOME/.pyenv/shims:$PATH"
echo "pyenv shims are now first in PATH. Python version: $(python3 --version)" 