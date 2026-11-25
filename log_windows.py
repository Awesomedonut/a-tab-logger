import argparse
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit


BROWSERS = {"Brave": "Brave Browser", "Chrome": "Google Chrome", "Safari": "Safari"}

CAPTURE = r'''
function run(argv) {
 const options=JSON.parse(argv[0]);
 const result={browsers:[],finder:[]};
 for(const name of options.browsers) {
  const app=Application(name);
  const entry={browser:name,running:app.running(),windows:[]};
  if(entry.running) {
   const windows=app.windows();
   for(let i=0;i<windows.length;i++) {
    const w=windows[i];
    const out={id:w.id(),index:i+1,private:false,tabs:[]};
    if(name!=="Safari")out.private=w.mode()==="incognito";
    const tabs=w.tabs();
    for(let j=0;j<tabs.length;j++) {
     const t=tabs[j];
     const tab={index:j+1,title:name==="Safari"?t.name():t.title(),url:t.url()};
     if(name!=="Safari")tab.id=t.id();
     out.tabs.push(tab);
    }
    entry.windows.push(out);
   }
  }
  result.browsers.push(entry);
 }
 if(options.finder) {
  const finder=Application("Finder");
  if(finder.running()) {
   for(const w of finder.windows()) {
    let url=null;
    let error=null;
    try {url=w.target().url();} catch(e) {error=String(e);}
    result.finder.push({id:w.id(),title:w.name(),url:url,error:error});
   }
  }
 }
 return JSON.stringify(result);
}
'''


def capture(browser_names, finder):
    result = subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", CAPTURE,
         json.dumps({"browsers": browser_names, "finder": finder})],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode:
        detail = result.stderr.strip() or "macOS automation failed"
        if "Application can't be found" in detail:
            probe = subprocess.run(["/bin/ps", "-axo", "comm="], capture_output=True, text=True, timeout=10)
            if probe.returncode:
                detail += "; process inspection also failed: " + probe.stderr.strip()
            detail += "; application discovery failed, so browser installation or running state is unconfirmed. Check current execution permissions; a browser connection is not required by this helper."
        raise RuntimeError(detail)
    return json.loads(result.stdout)


def markdown_urls(text):
    return set(re.findall(r"^- \[.*\]\((.*)\)$", text, re.MULTILINE))


def read_baselines(paths, explicit=False):
    urls = set()
    used = []
    for path in paths:
        path = path.expanduser().resolve()
        if path.suffix == ".json":
            data = json.loads(path.read_text())
            if data.get("schema") != "tab-logger-v1":
                if explicit:
                    raise ValueError(f"Unsupported baseline: {path}")
                continue
            found = {t["url"] for b in data["browsers"] for w in b["windows"] for t in w["tabs"]}
        else:
            found = markdown_urls(path.read_text())
        if explicit and not found and path.suffix != ".json":
            if not path.read_text().startswith("# New browser tabs"):
                raise ValueError(f"No browser URLs found in baseline: {path}")
        urls.update(found)
        used.append(str(path))
    return urls, used


def select_tabs(browsers, known, mode):
    selected = []
    stats = []
    seen = set()
    for browser in browsers:
        item = {**browser, "windows": []}
        total = saved = previous = duplicates = 0
        for window in browser["windows"]:
            tabs = []
            for tab in window["tabs"]:
                total += 1
                url = tab["url"]
                if not isinstance(url, str) or not url:
                    raise ValueError("A browser tab has no readable URL; capture is incomplete")
                if mode == "new" and url in known:
                    previous += 1
                elif mode == "new" and url in seen:
                    duplicates += 1
                else:
                    tabs.append(tab)
                    seen.add(url)
                    saved += 1
            if tabs:
                item["windows"].append({**window, "tabs": tabs})
        selected.append(item)
        stats.append({"browser": browser["browser"], "running": browser["running"],
                      "current_tabs": total, "saved_tabs": saved,
                      "previously_logged": previous, "duplicate_new_tabs": duplicates})
    return selected, stats


def escape_title(title):
    return title.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]").replace("\n", " ").replace("\r", " ")


def render(data):
    finder_only = data["finder_only"]
    title = "Finder windows" if finder_only else ("New browser tabs" if data["mode"] == "new" else "Browser tabs")
    if data["include_finder"] and not finder_only:
        title += " and Finder windows"
    lines = [f"# {title}", "", f"Date: {data['captured_at'][:10]}", "",
             f"Captured {data['captured_at']}. This is a snapshot only. Nothing was closed or changed.", ""]
    if not finder_only:
        lines += ["Comparison: exact URLs, preserving query strings and fragments. New-only mode excludes previously logged URLs and repeated new URLs.", ""]
        if data["baselines"]:
            lines += ["## Compared against", ""]
            lines += [f"- `{p}`" for p in data["baselines"]]
            lines.append("")
        lines += ["## Summary", ""]
        for s in data["stats"]:
            lines.append(f"- {s['browser']}: {s['saved_tabs']} saved, {s['previously_logged']} previously logged, {s['duplicate_new_tabs']} duplicate new tabs, {s['current_tabs']} currently open." if s["running"] else f"- {s['browser']}: not running.")
        lines += ["", f"Total: {sum(s['saved_tabs'] for s in data['stats'])} saved browser tabs.", ""]
        for browser in data["browsers"]:
            count = sum(len(w["tabs"]) for w in browser["windows"])
            if not count:
                continue
            lines += [f"## {browser['browser']} ({count} tabs)", ""]
            for window in browser["windows"]:
                kind = "Private window" if window["private"] else "Window"
                lines += [f"### {kind} {window['index']} ({len(window['tabs'])} tabs)", ""]
                lines += [f"- [{escape_title(t['title'])}]({t['url']})" for t in window["tabs"]]
                lines.append("")
    if data["include_finder"]:
        lines += [f"## Finder ({len(data['finder'])} windows)", "", "Each path is the folder exposed by that window; hidden Finder tabs are not captured.", ""]
        for i, window in enumerate(data["finder"], 1):
            parts = urlsplit(window["url"] or "")
            path = unquote(parts.path) if parts.scheme == "file" else None
            lines += [f"### Window {i}: {window['title']}", "", f"- Window ID: {window['id']}",
                      f"- Absolute path: `{path}`" if path else "- Absolute path: unavailable for this Finder view.", ""]
    return "\n".join(lines)


def save(data, directory, known):
    urls = [t["url"] for b in data["browsers"] for w in b["windows"] for t in w["tabs"]]
    if data["mode"] == "new":
        if set(urls) & known or len(urls) != len(set(urls)):
            raise ValueError("New-only verification failed")
    content = render(data)
    if markdown_urls(content) != set(urls):
        raise ValueError("Markdown URL verification failed")
    directory.mkdir(parents=True, exist_ok=True)
    prefix = "finder-windows" if data["finder_only"] else "new-browser-tabs" if data["mode"] == "new" else "browser-tabs"
    stamp = datetime.datetime.fromisoformat(data["captured_at"]).strftime("%Y-%m-%d_%H%M%S_%f")
    path = directory / f"{prefix}-{stamp}.md"
    companion = path.with_suffix(".json")
    data["verified"] = True
    for target, text in [(companion, json.dumps(data, ensure_ascii=False, indent=2) + "\n"), (path, content)]:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as output:
            output.write(text)
    if path.read_text() != content or json.loads(companion.read_text()) != data:
        raise ValueError("Saved-file verification failed; do not close tabs")
    return path, companion


def main():
    parser = argparse.ArgumentParser(description="Save macOS tabs and Finder paths without closing anything")
    parser.add_argument("--mode", choices=["new", "full"], default="new")
    parser.add_argument("--baseline", type=Path, action="append")
    parser.add_argument("--log-dir", type=Path, default=Path.home() / "Downloads/logs")
    parser.add_argument("--browser", choices=list(BROWSERS), action="append")
    parser.add_argument("--finder", action="store_true")
    parser.add_argument("--finder-only", action="store_true")
    parser.add_argument("--check-only", action="store_true", help="Read current tabs and report counts without saving logs")
    args = parser.parse_args()
    directory = args.log_dir.expanduser().resolve()
    names = [] if args.finder_only else [BROWSERS[b] for b in dict.fromkeys(args.browser or BROWSERS)]
    if args.check_only:
        snapshot = capture(names, args.finder or args.finder_only)
        _, stats = select_tabs(snapshot["browsers"], set(), "full")
        print(json.dumps({"access_verified": True, "logs_written": False, "browsers": stats,
                          "finder_windows": len(snapshot["finder"])}, ensure_ascii=False))
        return
    baselines = args.baseline or sorted(p for p in directory.glob("*")
                                      if p.suffix in (".md", ".json") and ("browser" in p.name.lower() or "tabs" in p.name.lower()))
    known, used = read_baselines(baselines, explicit=bool(args.baseline)) if args.mode == "new" and not args.finder_only else (set(), [])
    snapshot = capture(names, args.finder or args.finder_only)
    browsers, stats = select_tabs(snapshot["browsers"], known, args.mode)
    data = {"schema": "tab-logger-v1", "captured_at": datetime.datetime.now().astimezone().isoformat(),
            "mode": args.mode, "finder_only": args.finder_only, "include_finder": args.finder or args.finder_only,
            "baselines": used, "browsers": browsers, "stats": stats, "finder": snapshot["finder"]}
    path, companion = save(data, directory, known)
    print(json.dumps({"file": str(path), "companion": str(companion), "verified": True,
                      "saved_tabs": sum(s["saved_tabs"] for s in stats), "browsers": stats,
                      "finder_windows": len(data["finder"])}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        raise SystemExit(f"Tab logging failed: {error}")
