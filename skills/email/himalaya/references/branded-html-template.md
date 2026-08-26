# Branded HTML email template (Lucaryin)

Use this for **scheduled-work emails and any rich report/digest** — an audience
digest, a research brief, a weekly summary, an outreach recap. It turns a wall
of text into an on-brand, WOW email that survives real mail clients.

## How to use it

1. Copy the skeleton below and fill every `{{PLACEHOLDER}}`. Delete the sections
   you don't need (e.g. the metrics row) — every block is optional except the
   header and one content section.
2. Pass the finished HTML as the **`html`** argument of the `email_send` tool,
   and pass a plain-text version of the same content as **`body`** (the text
   fallback). `email_send` sends both as `multipart/alternative`.
3. **Do NOT add your own footer or signature.** The org signature footer is
   appended automatically by `email_send` (to both the text and HTML parts).
   Adding one yourself double-renders it.

## Hard rules (why emails look broken otherwise)

Mail clients (Gmail, Outlook, Apple Mail) **strip `<style>` blocks, external
CSS, web fonts, `<link>`, flexbox, grid, and CSS variables**. So:

- **Inline every style** on the element (`style="…"`). No `<style>` tag.
- **Lay out with `<table role="presentation">`**, not flex/grid.
- **Real text and real characters** only — no `::before`/`::after`, no icon fonts.
- **Light background.** This is design law (DESIGN.md): warm-cream canvas, white
  cards, ink headings, a single teal accent. Never a dark email.
- Font stack is **Work Sans → system fallback**. Work Sans won't load in most
  mail clients; the system fallback is expected and fine — keep the stack so it
  renders as Work Sans in clients that do allow it and clean system sans elsewhere.
- Keep it **under ~100KB**; don't embed big images. A wordmark in text beats a
  logo image that shows as a broken box when images are blocked.

## Brand tokens (hardcode these hex values inline)

| Role | Hex |
|------|-----|
| Canvas (page bg) | `#f8f6f3` |
| Card surface | `#ffffff` |
| Ink (headings, wordmark) | `#1a1a2e` |
| Teal accent (rules, links) | `#14b8a6` |
| Deep teal (link text) | `#0d9488` |
| Body text | `#374151` |
| Muted text | `#6b7280` |
| Hairline border | `#e5e7eb` |

Font stack (use verbatim):
`font-family:'Work Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;`

---

## Skeleton (fill the `{{…}}`)

```html
<!-- Outer canvas: full-width warm-cream ground, centered 600px column -->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;background-color:#f8f6f3;margin:0;padding:0;">
  <tr>
    <td align="center" style="padding:24px 12px;">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;width:100%;max-width:600px;">

        <!-- Header: wordmark + eyebrow + title -->
        <tr>
          <td style="padding:4px 4px 16px;font-family:'Work Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
            <div style="font-size:13px;font-weight:600;letter-spacing:0.06em;text-transform:uppercase;color:#0d9488;">Lucaryin&nbsp;AI</div>
            <div style="font-size:12px;color:#6b7280;margin-top:2px;">{{EYEBROW — e.g. Weekly audience &amp; positioning digest}}</div>
            <h1 style="margin:10px 0 0;font-size:24px;line-height:1.25;font-weight:700;color:#1a1a2e;">{{HEADLINE}}</h1>
          </td>
        </tr>

        <!-- Accent rule -->
        <tr><td style="padding:0 4px;"><div style="height:3px;background-color:#14b8a6;font-size:0;line-height:0;">&nbsp;</div></td></tr>

        <!-- TL;DR card (white surface) -->
        <tr>
          <td style="padding:16px 4px 0;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;background-color:#ffffff;border:1px solid #e5e7eb;border-radius:10px;">
              <tr>
                <td style="padding:16px 18px;font-family:'Work Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
                  <div style="font-size:12px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;color:#1a1a2e;">The short version</div>
                  <p style="margin:8px 0 0;font-size:15px;line-height:1.6;color:#374151;">{{TL;DR — 1–2 sentences: the single most important takeaway}}</p>
                </td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- Optional metrics row: 3 stat cards. Delete if not relevant. -->
        <tr>
          <td style="padding:12px 4px 0;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">
              <tr>
                <td width="33%" valign="top" style="padding:0 4px;">
                  <div style="background-color:#ffffff;border:1px solid #e5e7eb;border-radius:10px;padding:12px 14px;font-family:'Work Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
                    <div style="font-size:22px;font-weight:700;color:#1a1a2e;">{{STAT_1}}</div>
                    <div style="font-size:12px;color:#6b7280;margin-top:2px;">{{LABEL_1}}</div>
                  </div>
                </td>
                <td width="33%" valign="top" style="padding:0 4px;">
                  <div style="background-color:#ffffff;border:1px solid #e5e7eb;border-radius:10px;padding:12px 14px;font-family:'Work Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
                    <div style="font-size:22px;font-weight:700;color:#1a1a2e;">{{STAT_2}}</div>
                    <div style="font-size:12px;color:#6b7280;margin-top:2px;">{{LABEL_2}}</div>
                  </div>
                </td>
                <td width="33%" valign="top" style="padding:0 4px;">
                  <div style="background-color:#ffffff;border:1px solid #e5e7eb;border-radius:10px;padding:12px 14px;font-family:'Work Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
                    <div style="font-size:22px;font-weight:700;color:#1a1a2e;">{{STAT_3}}</div>
                    <div style="font-size:12px;color:#6b7280;margin-top:2px;">{{LABEL_3}}</div>
                  </div>
                </td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- Content section (repeat this block per section) -->
        <tr>
          <td style="padding:20px 4px 0;font-family:'Work Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
            <h2 style="margin:0 0 8px;font-size:17px;font-weight:700;color:#1a1a2e;">{{SECTION_HEADING}}</h2>
            <p style="margin:0 0 12px;font-size:15px;line-height:1.65;color:#374151;">{{PARAGRAPH — plain prose. Link like this: <a href="{{URL}}" style="color:#0d9488;text-decoration:none;font-weight:600;">{{LINK TEXT}}</a>.}}</p>
            <!-- Optional bullet list -->
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">
              <tr>
                <td width="16" valign="top" style="padding:2px 0 6px;color:#14b8a6;font-size:15px;line-height:1.65;">•</td>
                <td valign="top" style="padding:2px 0 6px;font-size:15px;line-height:1.65;color:#374151;font-family:'Work Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">{{BULLET}}</td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- Optional call-to-action button (bulletproof, no images) -->
        <tr>
          <td style="padding:20px 4px 8px;">
            <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">
              <tr>
                <td style="background-color:#14b8a6;border-radius:8px;">
                  <a href="{{CTA_URL}}" style="display:inline-block;padding:11px 22px;font-family:'Work Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:14px;font-weight:600;color:#ffffff;text-decoration:none;">{{CTA_LABEL}}</a>
                </td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- No footer here — email_send appends the org signature automatically. -->

      </table>
    </td>
  </tr>
</table>
```

---

## Worked example (the quality bar) — an audience & positioning digest

This is what "WOW, not decent" looks like when filled in:

```html
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;background-color:#f8f6f3;margin:0;padding:0;">
  <tr>
    <td align="center" style="padding:24px 12px;">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;width:100%;max-width:600px;">
        <tr>
          <td style="padding:4px 4px 16px;font-family:'Work Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
            <div style="font-size:13px;font-weight:600;letter-spacing:0.06em;text-transform:uppercase;color:#0d9488;">Lucaryin&nbsp;AI</div>
            <div style="font-size:12px;color:#6b7280;margin-top:2px;">Weekly audience &amp; positioning digest · Aug 26</div>
            <h1 style="margin:10px 0 0;font-size:24px;line-height:1.25;font-weight:700;color:#1a1a2e;">Your buyers stopped asking "what is it" and started asking "who's it for"</h1>
          </td>
        </tr>
        <tr><td style="padding:0 4px;"><div style="height:3px;background-color:#14b8a6;font-size:0;line-height:0;">&nbsp;</div></td></tr>
        <tr>
          <td style="padding:16px 4px 0;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;background-color:#ffffff;border:1px solid #e5e7eb;border-radius:10px;">
              <tr>
                <td style="padding:16px 18px;font-family:'Work Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
                  <div style="font-size:12px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;color:#1a1a2e;">The short version</div>
                  <p style="margin:8px 0 0;font-size:15px;line-height:1.6;color:#374151;">Owner-operators of 5–20 person shops are your sharpest-converting segment this week. Lead with "a team you hire," not "AI software" — the software framing drew curiosity but the team framing drew replies.</p>
                </td>
              </tr>
            </table>
          </td>
        </tr>
        <tr>
          <td style="padding:12px 4px 0;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">
              <tr>
                <td width="33%" valign="top" style="padding:0 4px;">
                  <div style="background-color:#ffffff;border:1px solid #e5e7eb;border-radius:10px;padding:12px 14px;font-family:'Work Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
                    <div style="font-size:22px;font-weight:700;color:#1a1a2e;">3.1×</div>
                    <div style="font-size:12px;color:#6b7280;margin-top:2px;">reply rate, "team" vs "software"</div>
                  </div>
                </td>
                <td width="33%" valign="top" style="padding:0 4px;">
                  <div style="background-color:#ffffff;border:1px solid #e5e7eb;border-radius:10px;padding:12px 14px;font-family:'Work Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
                    <div style="font-size:22px;font-weight:700;color:#1a1a2e;">5–20</div>
                    <div style="font-size:12px;color:#6b7280;margin-top:2px;">headcount of top segment</div>
                  </div>
                </td>
                <td width="33%" valign="top" style="padding:0 4px;">
                  <div style="background-color:#ffffff;border:1px solid #e5e7eb;border-radius:10px;padding:12px 14px;font-family:'Work Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
                    <div style="font-size:22px;font-weight:700;color:#1a1a2e;">$20</div>
                    <div style="font-size:12px;color:#6b7280;margin-top:2px;">price anchor that converted</div>
                  </div>
                </td>
              </tr>
            </table>
          </td>
        </tr>
        <tr>
          <td style="padding:20px 4px 0;font-family:'Work Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
            <h2 style="margin:0 0 8px;font-size:17px;font-weight:700;color:#1a1a2e;">Who's leaning in</h2>
            <p style="margin:0 0 12px;font-size:15px;line-height:1.65;color:#374151;">Owner-operators who already outsource bookkeeping or scheduling recognize the "hire" model instantly — they've bought help before and read this as more of the same, minus the hiring overhead.</p>
          </td>
        </tr>
        <tr>
          <td style="padding:8px 4px 0;font-family:'Work Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
            <h2 style="margin:0 0 8px;font-size:17px;font-weight:700;color:#1a1a2e;">What to say next</h2>
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">
              <tr>
                <td width="16" valign="top" style="padding:2px 0 6px;color:#14b8a6;font-size:15px;line-height:1.65;">•</td>
                <td valign="top" style="padding:2px 0 6px;font-size:15px;line-height:1.65;color:#374151;font-family:'Work Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">Lead every headline with the team, not the technology.</td>
              </tr>
              <tr>
                <td width="16" valign="top" style="padding:2px 0 6px;color:#14b8a6;font-size:15px;line-height:1.65;">•</td>
                <td valign="top" style="padding:2px 0 6px;font-size:15px;line-height:1.65;color:#374151;font-family:'Work Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">Name the segment in the subject line — "for owner-operators" beat generic by a wide margin.</td>
              </tr>
            </table>
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>
```

Text fallback (`body`) for the example above — send this too, one plain paragraph
per section, so non-HTML clients still read cleanly:

```
Weekly audience & positioning digest — Aug 26

THE SHORT VERSION
Owner-operators of 5–20 person shops are your sharpest-converting segment this
week. Lead with "a team you hire," not "AI software."

Who's leaning in: owner-operators who already outsource bookkeeping/scheduling
recognize the "hire" model instantly.

What to say next:
- Lead every headline with the team, not the technology.
- Name the segment in the subject line.
```

(The org signature is appended automatically — don't add one.)
