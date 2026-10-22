# Design Exercise: Relay

## About Relay

Relay is a seed-stage SaaS for B2B sales teams. Companies subscribe to Relay so their sales reps spend less time on admin. When a rep finishes a call with a prospect, the company's meeting tool sends Relay the transcript through a webhook. Relay uses an LLM to summarize the call and extract next steps, then updates the deal and creates follow-up tasks in the company's CRM (Salesforce or HubSpot). Each company connects its CRM to Relay through OAuth. Customers expect CRM updates within about 15 minutes of a call ending; it doesn't need to be real-time.

Throughout this exercise, "customer" means a company that subscribes to Relay.

## The application

Relay's code is containerized Python: a webhook API that receives transcripts, a worker that processes them and calls the LLM, and a small admin web app where customers connect their CRM and meeting tool and manage their settings. Everything is stored in one Postgres database: transcripts, summaries and extracted next steps, customer settings, and customers' CRM tokens. The application team owns this code.

## How it runs today

- 3 design-partner customers, about 500 calls a day
- Everything runs on GCP in a single project, with no separate environments
- The team deploys manually from engineers' laptops
- A worker service processes each transcript and calls the LLM provider directly, using one shared API key
- Customers' CRM tokens are stored in the application database
- Transcripts, summaries and logs are kept indefinitely

## In 12 months

- Around 50 customers and 20,000 calls a day
- Enterprise customers who require that their data is logically isolated from other customers, never used to train models, and deleted on request. No customer requires dedicated infrastructure.
- Some customers want to restrict which LLM providers can see their data
- A SOC 2 audit planned for next quarter

## Team

Four engineers, including a data engineer who owns the pipeline code. Two are contractors working remotely on their own laptops. Two engineers are comfortable with containers, but no one knows Kubernetes deeply. No one has owned infrastructure until now.

## Your role

You're Relay's first DevOps engineer. Assume the application code already exists and works. Your job is the platform it runs on.

## The exercise

Design the platform for Relay's application. Use one call's journey, from the webhook arriving to the task landing in the customer's CRM, to walk us through your design and the reasoning behind it.

Then tell us the first three things you'd fix from how it runs today, and why.

Relay already runs on GCP. If you'd strongly recommend a different cloud, make the case briefly.

You'll have about 20 minutes. Think out loud and ask any clarifying questions. We care more about your reasoning and priorities than about covering everything.
