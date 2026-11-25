# tab-logger

Save browser tabs to Markdown and JSON on macOS. Supports Brave, Chrome and Safari, with optional Finder paths. Includes private windows.

```sh
python3 log_windows.py --browser Brave
python3 log_windows.py --browser Brave --finder --mode full
```

Defaults to saving only new URLs in `~/Downloads/logs`. Requires Python 3 and macOS Automation permission. Run with `--help` for options.
