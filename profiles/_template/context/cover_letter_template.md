# Cover letter template

This file is the written template that every drafted cover letter follows.
Copy it into your profile's `context/` folder and replace the bracketed
formulas with your own wording, taken from your past letters. Your past
letters in `context/` give the **voice**; this file gives the **structure and
rules**. When the two conflict, this file wins.

---

## 1. Letterhead (rendered by code: never type it)

The renderer builds the letterhead from `profile.yaml`, using today's date.
The saved letter text **starts at the salutation**. Never type your name,
address, phone, email or a date line above it.

Rendered layout (US Letter, 1" margins, left-aligned, one typeface throughout):

| Line | Content | Formatting |
|---|---|---|
| 1 | `{full_name}` | Large (about 26 pt), left-aligned, not centered |
| 2 | `{address}, {city} {state_abbrev} {zip} \| {phone} \| {email}` | 11 pt, one line, segments joined by `" \| "` |
| 3 | date as `M/D/YYYY`, not zero-padded | 11 pt |
| 4 | (blank) then the salutation | |

There is about one blank line between the letterhead lines and before the
salutation, and one blank line between body paragraphs.

## 2. Salutation (exact form)

```
To the <Company> Hiring Team,
```

## 3. Paragraph skeleton (4-5 body paragraphs)

The order is: opener, then 2-3 evidence paragraphs, then the closer.

### P1: Opener (one canonical formula; do not alternate)

> "I recently came across the posting for the <Role> role. I am certain that my
> background in <your two core strengths> makes me a perfect fit for your team.
> I have built my career on <your career line>, leveraging a blend of <skill A>
> and <skill B>."

- Copy `<Role>` exactly as the posting gives it.
- Optionally add one sentence: "The mandate to <JD phrase> aligns perfectly
  with my expertise as <your identity>."
- Do not open with "I'm writing to express my strong interest" or "What draws
  me to this opportunity", and do not switch between openers from one letter
  to the next.

### P2-P4: Evidence paragraphs (2 or 3)

Each paragraph takes one JD requirement and backs it with one situation,
action and result story from `stories.md` / `background.md`.

- **P2, the JD mirror plus the flagship story.** "The role involves <JD
  responsibility>, a challenge that requires <capability>. At <Employer>, I
  demonstrated this capability by <action>. ... The result was <metric>."
- **P3, infrastructure / execution.** "I understand that <JD goal> requires
  <what it takes>. I have a proven track record of <how>. For example, I
  <story>."
- **P4 (optional), technical self-sufficiency.** "Additionally, my technical
  proficiency in <tools> allows me to be self-sufficient in <JD domain>. At
  <Employer>, I <example and result>."

How the JD is mirrored: restate the posting's own nouns (the team, the mandate
and the domain). Quote at most one distinctive JD phrase per letter. Never
paste JD bullet lists.

How metrics are phrased: use digits and `%`, written exactly as
`background.md` gives them (for example `~N%` for approximate figures). Do not
write "roughly N%" or "nearly zero". Use one headline metric per evidence
paragraph, and never invent a number.

### P5: Closer (its own paragraph)

> "I excel at <your signature strengths>. I am eager to bring my uniquely
> suited skillset as <your self-descriptor> to help <Company / the <Team>
> team> <specific outcome from the JD>."

Do not add "I would welcome the chance to discuss..." or "Thank you for your
consideration." unless your own letters actually do.

### Sign-off (exact)

```
Sincerely,
<full_name>
```

## 4. Length band

- **Target: 295-425 body words**, counted from the line after the salutation
  to the line before "Sincerely,".
- **Hard limits: 270-460.**
- Recalibrate both against your own letters if they run consistently shorter
  or longer.

## 5. Banned constructs

- **Em dash `—`, en dash `–` and `--`.** Use a comma, a semicolon or a new
  sentence instead.
- **Typed letterhead** (name, address, phone, email or date above the
  salutation).
- **Generic openers:** "I'm writing to express my strong interest", "What
  draws me to this opportunity".
- **Parenthetical asides** `( ... )`.
- **"0-to-1", "zero to one", "0→1"**, unless your own letters use that exact
  phrase.
- **AI-tell phrasing absent from your own letters:** "genuinely", "rather
  than", "roughly N%", "nearly/near zero", aphoristic punch lines ("That is my
  current job.", "I would rather show than claim."), more than one colon
  reveal ("X is this: Y").
- **Contractions,** unless your own letters use them.
- **More than 5 body paragraphs.**

## 6. Tense rule

Follow the **Career timeline** section of `context/background.md` for role
order and tense. Present tense is allowed only for the role it marks CURRENT,
while that role is current. Ended roles are written in the past tense. Do not
copy dates or employers into this file; re-read the timeline for each letter.

## 7. Annotated example

Build this section from verbatim sentences in one of your own letters, with
each paragraph labeled P1-P5 as in section 3. Do not write new facts here.
