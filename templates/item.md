[&larr; All books]($home)

# $A

<p class="meta">by <strong>$B</strong> &middot; first published $C &middot; ${Language}</p>

$G

{{#if H}}
[Read it for free on Project Gutenberg &rarr;]($H)
{{/if}}

{{#if D == "Novel"}}
> This is one of the **{{count}}** works in the catalog, and it is a full-length novel.
{{else}}
> This is one of the **{{count}}** works in the catalog ($D).
{{/if}}

## Tags

{{#each Tags}}
- $this
{{else}}
No tags yet.
{{/each}}

## All fields

{{fields}}

{{pager}}
