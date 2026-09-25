#!/bin/zsh
# stdio entry point: ssh macmini ~/shop-mcp/run.sh
export PATH=/opt/homebrew/bin:$PATH
exec uv run --quiet --script "$HOME/shop-mcp/server.py"
