# catalog-generator

Turn a spreadsheet into a static, searchable catalog website: one row per item.

- **Overview page** listing every item, with search, filters and sorting (works offline, no server needed).
- **Detail page** for every item.
- Pages are written in **Markdown** with simple template markup: `$A` is the value of column A for the current row.
- Data comes from a **CSV file** or a **Google Sheet**.
- Output is plain HTML/CSS/JS that you can host on **GitHub Pages** (workflow included) or anywhere else.
- **One file, no dependencies**: `catalog.py` only needs Python 3.8+, which ships with macOS and Linux and is a one-click install on Windows.

## Quick start

```bash
git clone https://github.com/pauginer/catalog-generator.git
```

```bash
cd catalog-generator
```

```bash
python3 catalog.py serve
```

Open http://localhost:8000. The page rebuilds when you edit the data, config or templates; reload the browser to see changes.
On Windows, use `py` instead of `python3`.

To start a fresh catalog in another folder (it copies `catalog.py` there too, so the folder is self-contained):

```bash
python3 catalog.py init ../my-catalog
```

| Command | What it does |
|---|---|
| `python3 catalog.py init [folder]` | Create a project with example data, templates and a GitHub Pages workflow. Existing files are never overwritten. |
| `python3 catalog.py build` | Generate the site into `_site/`. |
| `python3 catalog.py serve` | Build, serve on http://localhost:8000 and rebuild on changes. `--port`, `--no-watch`. |

`build` and `serve` also take `--config <file>`, `--out <folder>` and `--source <csv path or sheet URL>`.

## Project layout

```
catalog.json          settings (title, data source, filters, sorting...)
data/items.csv        the spreadsheet: first row = column headers
templates/
  index.md            overview page; {{catalog}} marks where the searchable list goes
  card.md             one item in the overview list
  item.md             an item's detail page
  page.md             wrapper for extra pages
  layout.html         HTML shell shared by every page
  style.css           your CSS overrides (added after the built-in styles)
pages/*.md            extra pages, e.g. pages/about.md -> about.html
static/               optional: copied as-is to the site root (images, favicon, CNAME)
```

Any template you delete falls back to a built-in default, so a project can be just `catalog.json` plus a CSV.

## Using a Google Sheet

1. In Google Sheets, click **Share** and set *General access* to **Anyone with the link: Viewer**
   (or use *File > Share > Publish to web*).
2. Paste the sheet's normal URL as the `source` in `catalog.json`:

   ```json
   "source": "https://docs.google.com/spreadsheets/d/1AbC.../edit#gid=0"
   ```

The `gid` in the URL picks the tab. You can also pick a tab by name with `"sheet": "Books"`.
The data is downloaded on each build. To refresh the website when the sheet changes, re-run the GitHub workflow
(Actions tab > *Deploy catalog* > *Run workflow*) or enable its daily schedule.

If you'd rather not commit the sheet URL, store it as a repository secret named `CATALOG_SOURCE` and uncomment the
`env` lines in `.github/workflows/deploy.yml`; the `CATALOG_SOURCE` environment variable overrides `source`.

## Template syntax

Templates are Markdown. Before the Markdown is converted, these placeholders are replaced with values from the current row:

| Markup | Meaning |
|---|---|
| `$A`, `$B` ... `$AA` | Value of that spreadsheet column |
| `${A}` | Same, when followed by letters: `${A}s` |
| `${Author}` or `{{Author}}` | Value of the column with that header (case-insensitive) |
| `$$` | A literal `$` |
| `{{#if A}} ... {{else}} ... {{/if}}` | Show content only when the column is not empty |
| `{{#if Genre == "Novel"}} ... {{/if}}` | Compare with a value (case-insensitive); `!=` also works |
| `{{#unless A}} ... {{/unless}}` | Opposite of `#if` |
| `{{#each Tags}} $this {{/each}}` | Repeat for each value in a comma-separated cell. `$index`, `$first`, `$last` are available. `{{else}}` covers the empty case. |

Built-in variables (lowercase):

| Variable | Value |
|---|---|
| `$title` | The item title (the `title_column`) |
| `$url` | Link to the item's detail page, relative to the current page |
| `$home` | Link to the overview page |
| `$root` | Relative path to the site root, (`""` or `"../"`), for files in `static/`: `![](${root}logo.png)` |
| `$id`, `$index`, `$row` | Item slug, 1-based position, spreadsheet row number |
| `$site_title`, `$description`, `$count` | From `catalog.json`; `$count` = number of items |
| `{{fields}}` | (detail page) a table with every non-empty column |
| `{{pager}}` | (detail page) previous/next links |
| `$prev_url`, `$prev_title`, `$next_url`, `$next_title` | (detail page) for building your own navigation |
| `{{catalog}}` | (overview page) the search box, filters and item cards |

Cell values may contain Markdown (`**bold**`, links), and bare URLs become links. HTML in cells is escaped, so a
spreadsheet cannot inject markup; HTML written in the templates themselves is kept, e.g.
`<span class="tag">$this</span>`. A variable that matches nothing (like `$X` when there is no column X) is left
as typed and reported as a warning during the build.

Example `templates/card.md`:

```markdown
### [$title]($url)

<p class="meta">$B · $C</p>

{{#if Image}}![]($Image){{/if}}

{{#each Tags}}<span class="tag">$this</span>{{/each}}
```

## Configuration (`catalog.json`)

| Key | Default | Description |
|---|---|---|
| `title` | `"My Catalog"` | Site title |
| `description` | `""` | Used in `<meta name="description">` and as `$description` |
| `language` | `"en"` | `<html lang>` |
| `source` | `"data/items.csv"` | CSV/TSV path (relative to the config) or a Google Sheets / CSV URL |
| `sheet` | – | Google Sheets tab name |
| `delimiter` | auto | Force a CSV delimiter, e.g. `";"` |
| `title_column` | `"A"` | Column used for `$title`, page titles and file names |
| `id_column` | – | Column with stable unique IDs for page file names (default: derived from the title) |
| `list_separator` | `","` | Separator for multi-value cells (`#each`, multi-value filters) |
| `search_columns` | all except link-only cells | Columns the search box looks in, e.g. `["A", "Author"]` |
| `filters` | `[]` | Drop-down filters: `{"column": "D", "label": "Genre", "multiple": true}`. `multiple` splits the cell with `list_separator`. |
| `sort` | `[]` | Sort options: `{"column": "C", "label": "Newest", "numeric": true, "descending": true}` |
| `nav` | `[]` | Header links: `{"label": "About", "url": "about.html"}` |
| `footer` | `""` | Footer text (inline Markdown) |
| `head` | `""` | Extra HTML for `<head>` (analytics, fonts...) |
| `output` | `"_site"` | Output folder |
| `templates_dir`, `pages_dir`, `static_dir` | `templates`, `pages`, `static` | Folder names |

Columns can be referenced by letter (`"D"`) or by header (`"Genre"`) everywhere.

Search filters the list as you type. It ignores case and accents, an item must contain every word typed, and
matches are highlighted on the cards. When a word matches a column the card doesn't show (say, Genre), the card gets
a short "Genre: Novel" line explaining the match. Cells that contain only a link (web pages, images) are not
searched, so a word like "book" doesn't match every item through its URL; list a column in `search_columns` to
search it anyway. The current search, filters and sort are kept in the URL,
so a filtered view can be bookmarked or shared.

## Publishing on GitHub Pages

1. Push the project to a GitHub repository (the `main` branch).
2. In the repository go to **Settings > Pages** and set **Source** to **GitHub Actions**.
3. The included workflow (`.github/workflows/deploy.yml`) builds and publishes the site on every push.
   Your catalog appears at `https://<user>.github.io/<repo>/`.

All links are relative, so the site works under a sub-path, on any static host, or opened straight from disk.

Prefer committing the HTML instead? Run `python3 catalog.py build --out docs` and choose *Deploy from a branch > /docs*
in the Pages settings. The output folder is fully replaced on each build, so keep files like `CNAME` in `static/`.

## Notes and limits

- The Markdown converter is built in (headings, emphasis, links, images, lists, task lists, quotes, code, tables,
  raw HTML). It covers the common syntax, not every CommonMark edge case.
- All cards are rendered into the overview page, which stays fast up to a few thousand items.
- Every build replaces the output folder. As a safeguard, the tool refuses to overwrite a non-empty folder it didn't
  create unless you pass `--force`.
