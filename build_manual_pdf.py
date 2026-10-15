from pathlib import Path
import markdown

root = Path(__file__).parent
source = (root / 'USER_MANUAL.md').read_text(encoding='utf-8')
body = markdown.markdown(
    source,
    extensions=['tables', 'fenced_code', 'sane_lists'],
    output_format='html',
)
html = f'''<!doctype html>
<html><head><meta charset="utf-8"><title>Industrial Production Monitor - User Manual</title>
<style>
@page {{ size: A4; margin: 18mm 16mm 18mm 16mm; @bottom-right {{ content: "Page " counter(page); font-size: 9pt; color: #666; }} }}
* {{ box-sizing: border-box; }}
body {{ font-family: Arial, Helvetica, sans-serif; color: #202124; line-height: 1.42; font-size: 10.5pt; }}
h1 {{ color: #123b63; font-size: 24pt; margin: 0 0 8pt; border-bottom: 2px solid #2f75b5; padding-bottom: 6pt; }}
h2 {{ color: #174f7c; font-size: 16pt; margin-top: 20pt; page-break-after: avoid; border-bottom: 1px solid #b9cde0; padding-bottom: 3pt; }}
h3 {{ color: #245d84; font-size: 12.5pt; margin-top: 13pt; page-break-after: avoid; }}
p, ul, ol {{ margin: 5pt 0 8pt; }}
ul, ol {{ padding-left: 20pt; }}
li {{ margin: 2pt 0; }}
table {{ width: 100%; border-collapse: collapse; margin: 9pt 0 13pt; font-size: 9.2pt; page-break-inside: avoid; }}
thead {{ display: table-header-group; }}
th {{ background: #dbeaf7; color: #163d5d; font-weight: bold; text-align: left; }}
th, td {{ border: 0.6pt solid #8ca7bd; padding: 5pt 6pt; vertical-align: top; }}
tr:nth-child(even) td {{ background: #f5f8fb; }}
code {{ font-family: 'DejaVu Sans Mono', monospace; background: #f0f2f4; padding: 1pt 3pt; border-radius: 2pt; }}
pre {{ background: #f3f5f7; border: 0.6pt solid #ccd5dc; padding: 7pt; white-space: pre-wrap; font-size: 8.5pt; page-break-inside: avoid; }}
strong {{ color: #123b63; }}
blockquote {{ border-left: 3pt solid #2f75b5; padding-left: 9pt; color: #4c5964; }}
hr {{ border: none; border-top: 1px solid #bbb; }}
</style></head><body>{body}</body></html>'''
(root / 'USER_MANUAL.html').write_text(html, encoding='utf-8')
print(root / 'USER_MANUAL.html')
