# fitness-agent

I text my workouts the way I'd text a friend, and an agent turns them into
structured rows in a spreadsheet I own.

## Why I built it

I like to have data that lets me track progress make sure my workouts are going in the direction I expect. I would rather not maintain a notebook (first choice) or a spreadsheet on google sheets on my phone. I would also like to be able to quickly sort and check my data.

I did not want to lock myself to a specific user experience, ie tap the exercise, tap the set, tap
the weight, pick from a dropdown that doesn't have "Australian pull ups" in it. So I decided to build a light weight system that let me keep the input an structured blob of text that an LLM will then translate into strutured data on Google Sheets.  

The solution I built runs on DigitalOcean Functions' free tier against a free LLM, so there's no server sitting idle and no subscription. 

## Example
```
me ▸  Squats 4x7 60lbs on each side
      3.5 real pull ups
      Row Machine 15 reps of 35lbs then 15 of 65lbs

      ┌─ Log this workout? ──────────────────────────┐
      │ Back Squat     4 × 7   60/side    165 lbs    │
      │ Pull Up        1 × 3.5            bodyweight │
      │ Row Machine    1 × 15             35 lbs     │
      │ Row Machine    1 × 15             65 lbs     │
      │                                    ✅   ❌    │
      └──────────────────────────────────────────────┘

me ▸  am I progressing on squats?
      Top set is up 20 lbs over 8 weeks — 145 → 165. [chart]
```

To make sure the LLM output isn't drifint I have a golden dataset collected in
[`evals/golden/README.md`](evals/golden/README.md)

## Docs 

The Docs are AI generated, point a code agent at those to set it up for yourself.

- **[Set it up for yourself](docs/SETUP.md)** — full walkthrough: Google service
  account, config, running locally, deploying.
- **[deploy/RUNBOOK.md](deploy/RUNBOOK.md)** — day-2 operations and the
  platform gotchas worth knowing before you change deploy config.
- **[evals/golden/README.md](evals/golden/README.md)** — the real messages that
  define correct parsing.

## Security

It's on the public internet but there is nothing important to secure so I have a simple secuirity model where a single passphrase is the door to accessing the full application. All components depending on payments (OpenRouter and DigitalOcean have strict wallet limits so if you self host I suggest you do that too). In this set up the worst case for a leaked passphrase is junk rows in a sheet and a burned free-tier LLM quota.
## Status

## License

[PolyForm Noncommercial 1.0.0](LICENSE). You can read it, run it for yourself, and
modify it for your own use, free. You can't sell it, host it as a paid service, or
build a commercial product on it. This is source-available, not open source — the
license restricts commercial use, which OSI-approved licenses can't.
