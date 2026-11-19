"""Builds the comparison web page: a summary table, then one card per posting with every config and run side by side."""

import html
from collections import Counter

COLOURS = {"apply": "#1a7f37", "stretch": "#b7791f", "skip": "#c0392b",
           "lack_of_info": "#2b6cb0", "error": "#6b7280", "no majority": "#6b7280", "-": "#9ca3af"}


def _badge(verdict):
    colour = COLOURS.get(verdict, "#6b7280")
    return f'<span class="badge" style="background:{colour}">{html.escape(verdict)}</span>'


def _esc(text):
    return html.escape(str(text))


def build_report(summary, postings, rows, labels, config_names, rule, chosen, out_path):
    by_key = {(r["config"], r["refnr"], r["run"]): r for r in rows}
    runs = sorted({r["run"] for r in rows}) or [1]
    stats = {s["config"]: s for s in summary}

    # postings where the configs' majority verdicts differ come first: those are the ones that need a label
    def disagreement(p):
        majorities = {stats[c]["majority"].get(p["refnr"]) for c in stats}
        return len(majorities)

    ordered = sorted(postings, key=lambda p: (-disagreement(p), p["title"]))

    out = [f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Model comparison</title>
<style>
:root {{ --bg:#ffffff; --fg:#111827; --muted:#6b7280; --line:#e5e7eb; --card:#f9fafb; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#111418; --fg:#e5e7eb; --muted:#9ca3af; --line:#2d333b; --card:#181c22; }} }}
body {{ background:var(--bg); color:var(--fg); font:15px/1.5 system-ui, sans-serif; margin:0 auto; max-width:1200px; padding:16px; }}
h1 {{ font-size:22px; }} h2 {{ font-size:17px; margin:0 0 4px; }}
.muted {{ color:var(--muted); }}
table {{ border-collapse:collapse; width:100%; margin:8px 0 16px; }}
th, td {{ border:1px solid var(--line); padding:5px 8px; text-align:left; vertical-align:top; }}
th {{ background:var(--card); }}
.badge {{ color:#fff; border-radius:4px; padding:1px 7px; font-size:13px; white-space:nowrap; }}
.card {{ border:1px solid var(--line); border-radius:8px; padding:12px; margin:14px 0; background:var(--card); }}
.pass {{ color:#1a7f37; font-weight:600; }} .fail {{ color:#c0392b; font-weight:600; }}
.scroll {{ overflow-x:auto; }}
details summary {{ cursor:pointer; color:var(--muted); }}
.gap {{ font-size:13px; }}
</style></head><body>
<h1>Model comparison</h1>
<p class="muted">{_esc(rule)}</p>
<p><b>Rule picks:</b> {_esc(chosen or "nothing passes (yet)")}</p>
<div class="scroll"><table><tr><th>config</th><th>errors</th><th>correct</th><th>stable</th><th>eligibility caught</th>
<th>quotes verified</th><th>code = model verdict</th><th>$ per posting (now)</th><th>$ 2027</th><th>median</th><th>p90</th><th>rule</th></tr>"""]
    for s in summary:
        out.append(f"<tr><td>{_esc(s['config'])}</td><td>{s['errors']}</td><td>{s['correct']}/{s['labelled']}</td>"
                   f"<td>{s['stable']}/{s['postings']}</td><td>{s['elig_caught']}</td>"
                   f"<td>{s['quotes_verified_pct']:.1f}%</td><td>{s['agree_pct']:.0f}%</td>"
                   f"<td>{s['cost_now']:.4f}</td><td>{s['cost_2027']:.4f}</td><td>{s['median_s']:.1f} s</td>"
                   f"<td>{s['p90_s']:.1f} s</td><td class=\"{'pass' if s['passes'] else 'fail'}\">"
                   f"{'pass' if s['passes'] else 'fail'}</td></tr>")
    out.append("</table></div>")

    for p in ordered:
        ref = p["refnr"]
        label = labels.get(ref)
        label_html = (f"Label: {_badge(label['verdict'])} <span class=\"muted\">{_esc(label.get('note', ''))}</span>"
                      if label else '<span class="muted">No label yet</span>')
        n_majorities = disagreement(p)
        flag = "configs disagree" if n_majorities > 1 else "all configs agree"
        out.append(f'<div class="card"><h2>{_esc(p["title"])}</h2>'
                   f'<div class="muted">{_esc(p.get("firma") or "")} · {_esc(ref)} · {flag}</div>'
                   f'<p>{label_html}</p><div class="scroll"><table><tr><th>config</th>')
        out.extend(f"<th>run {run}</th>" for run in runs)
        out.append("<th>majority</th><th>critical gaps (run 1)</th></tr>")
        for c in config_names:
            if c not in stats:
                continue
            out.append(f"<tr><td>{_esc(c)}</td>")
            for run in runs:
                r = by_key.get((c, ref, run))
                v = "-" if r is None else ("error" if r["error"] else r["result"]["code_verdict"])
                out.append(f"<td>{_badge(v)}</td>")
            majority = stats[c]["majority"].get(ref, "-")
            right = "" if not label else (" ✓" if majority == label["verdict"] else " ✗")
            first = by_key.get((c, ref, runs[0]))
            gaps = ""
            if first and not first["error"]:
                gap_list = first["result"]["critical_gaps"] + [f"not_stated: {g}" for g in first["result"]["missing_eligibility"]]
                gaps = "<br>".join(_esc(g) for g in gap_list) or '<span class="muted">none</span>'
                if first["result"]["unverified_quotes"]:
                    gaps += f'<br><span class="muted">unverified quotes: {len(first["result"]["unverified_quotes"])}</span>'
            elif first:
                gaps = _esc(first["error"][:200])
            out.append(f"<td>{_badge(majority)}{right}</td><td class=\"gap\">{gaps}</td></tr>")
        out.append("</table></div>")
        out.append(f"<details><summary>Posting text</summary><pre style=\"white-space:pre-wrap\">"
                   f"{_esc(p['description'])}</pre></details></div>")

    out.append("</body></html>")
    out_path.write_text("".join(out), encoding="utf-8")
    return out_path
