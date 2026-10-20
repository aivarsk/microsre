import html
import sys
from pathlib import Path

from microsre import registry

BEGIN = "<!-- metrics:table:begin -->"
END = "<!-- metrics:table:end -->"


def table():
    rows = ["<p>Every metric, exactly where it comes from, and how it is measured:</p>", "", "<table>"]
    rows.append("<tr><th>Metric</th><th>Source file</th><th>Semantics</th></tr>")
    family = None
    for fam, display, names, prom, source, fields, alertable, semantics in registry.ROWS:
        if fam != family:
            family = fam
            rows.append(f'<tr><td class="group" colspan="3">{html.escape(fam)}</td></tr>')
        rows.append(
            f"<tr><td><code>{html.escape(display)}</code></td>"
            f"<td><code>{html.escape(source)}</code></td>"
            f"<td>{html.escape(semantics)}</td></tr>"
        )
        rows.append(f'<tr class="fields"><td colspan="3">{html.escape(fields)}</td></tr>')
    rows.append("</table>")
    rows += ["", "<h4>Caveats</h4>", "<ul>"]
    for family, text in registry.CAVEATS:
        rows.append(f"<li><strong>{html.escape(family)}:</strong> {html.escape(text)}</li>")
    rows.append("</ul>")
    return "\n".join(rows)


def main():
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/readme.html")
    text = path.read_text()
    begin = text.index(BEGIN) + len(BEGIN)
    end = text.index(END)
    path.write_text(text[:begin] + "\n" + table() + "\n" + text[end:])
    print(f"Updated {path}")


if __name__ == "__main__":
    main()
