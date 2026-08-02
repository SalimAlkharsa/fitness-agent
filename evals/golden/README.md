# Golden set source material

These are my real workout messages, edited only to alter the numbers — the
wording, formatting, and quirks are untouched, and they define what "parsed
correctly" means. Each is paired with a hand-verified expected-output fixture
(`*.json`, checked against the schema in `DESIGN.md`), and together they're what
`make eval MODEL=<id>` scores a model against.

They're here because parsing quality is the whole product, and the only honest way
to tell whether a prompt or model change helped is to score it against messages
that actually gave it trouble. The quirks each one exercises are noted below.

Setting this up for yourself means replacing these with your own logs — see
"Making it yours" in [`docs/SETUP.md`](../../docs/SETUP.md).

## Message 1 — mixed session, alias variety
```
4x8 60LBs Squats on Both Sides
2x15 20LBs roman glute swing
3x8 Australian Pull Ups
Bar Row 3x15 20Lbs on each side
Calf Raises 20x85lbs on each side 3 sts
```
Quirks: "on Both Sides"/"on each side" = per-side weight; "3 sts" = 3 sets;
"20x85lbs ... 3 sts" = 3 sets of 20 reps at 85/side; bodyweight Australian pull-ups.

## Message 2 — fractional reps, negatives
```
- 3.5 Real Pull Ups 4 negative pull ups
- 2.5 Real Pull ups 4 negative pull ups
- 8 Australian pull ups
```
Quirks: reps=3.5 and 2.5 (floats); negatives are a separate exercise; each line is a
set (Pull Up appears as two set-groups or per-set rows per schema grain).

## Message 3 — same exercise, two weights
```
3x60 lbs on each side squats x7
1x30 lbs on each side squats x8
```
Quirks: "3x60 ... x7" = 3 sets, 60/side, 7 reps. Two set-groups (grp 1, grp 2).

## Message 4 — smith machine, then-pattern rows
```
Calf raises on smith machine elevated
20x 75lbs on each side
3x20x 85 lbs on each side

Bar rows 3x12 20 lbs on each side then 3x15 20 lbs on each side
```
Quirks: "elevated" variant = same canonical exercise (KB note); "20x 75lbs" = 1 set of
20 at 75/side; "3x20x 85" = 3 sets of 20 at 85/side; "then" = second set-group.

## Message 5 — hex bar
```
Deadlift 3x10 hex bar 65lbs on each side
```
Quirks: hex bar base weight comes from KB, not the olympic-bar 45.

## Message 6 — prose style, stack machine
```
Squats 5 sets of 6 doing 60lbs on each side

Calf Raises on Smith Machine doing 75 on each side 4 sets of 20

Soleus raise on smith machine doing 1 set 30 on each side 15 reps then 3 sets of 60 on each side 15 reps

Hip abductor outer doing 3 sets of 10 at 145lbs
```
Quirks: fully prose phrasing; soleus has two set-groups; hip abductor is a stack
machine — 145 is the TOTAL, no per-side math.

## Message 7 — relative date, second activity
```
Need to log some workouts

Yesterday 5 to 7 pm

Bench 4x7, 40 lbs on each side
Bench assisted elevated leg dips 4x10
Tricep push downs 3x10
Shoulder press 3x10 15lbs in each side

Then basketball today 2:30 to 4:20 pm
```
Quirks: "Yesterday 5 to 7 pm" resolves against message timestamp → workout date +
start/end; TWO workouts in one message (strength yesterday, basketball today);
basketball = workouts row with type=basketball, no sets; tricep push downs have no
stated weight (leave weight blank, don't invent).

## Message 8 — assist weight
```
Log this work out Bench 5 sets of 6 doing 40lbs on each side

Bench 3 sets of 10 close grip 15s on each side

Cable Push Downs 3 sets of 10 doing 15lbs

Dips 4x8 95lbs assist
```
Quirks: "close grip" bench is a distinct exercise (Close-Grip Bench Press); "15s" =
15lbs per side; cable push downs 15lbs is the stack/total; "95lbs assist" →
assist_lbs=95 on bodyweight dips.
