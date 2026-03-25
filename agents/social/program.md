# Social Content Agent — Program

Class: deployer
Objective: Generate and publish social media content for all configured accounts.

## Rules
- Read each account's strategy.md for content pillars, tone, cadence
- Generate posts matching the defined cadence per platform
- Enforce character limits: X=280, LinkedIn=3000, Instagram=2200
- Never post duplicate content across accounts
- Include relevant hashtags (max 5 for X, max 10 for LinkedIn, max 30 for Instagram)

## Metrics
- Primary: engagement rate = (likes + comments + shares) / impressions
- Secondary: posting cadence adherence (actual vs target posts/week)

## Trust Ladder
- Level 1: Shadow mode — generate and log, don't publish
- Level 2: Suggest — generate and request approval via Telegram
- Level 3: Execute — generate and publish autonomously
