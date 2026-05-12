# MCO System — Plain English Guide
### What it is, how it works, and why we built it this way

---

## The Problem We Were Solving

Before this system, every channel your team uses to reach leads — LinkedIn, email, phone calls — worked in complete isolation.

If someone replied to your LinkedIn message on Monday, and then replied to one of your emails on Wednesday, the AI agent replying to the email had no idea about the LinkedIn conversation. It was starting from scratch every time. That makes your outreach feel robotic and disconnected.

The MCO system is the connective tissue. It gives every channel a shared memory, so your AI agents always know the full history of a lead — regardless of where the conversation started.

---

## Think of It Like a Reception Desk

Imagine your business has a reception desk that:
- Keeps a record every time anyone speaks to a lead, on any channel
- Can instantly pull up that lead's full history when anyone needs it
- Automatically schedules follow-ups across the right channels at the right time
- Makes sure no lead falls through the cracks

That reception desk is the MCO system.

---

## The Two Types of Leads

Everything in the system flows differently depending on where the lead first came from.

### Lead Source: LinkedIn (Aimfox)
Someone responded to one of your LinkedIn outreach messages. You already have a conversation going with them on LinkedIn inside Aimfox.

### Lead Source: Email (ByZone)
Someone responded to one of your cold outreach emails sent via ByZone. You have their email address but you may not know their LinkedIn profile yet.

The system handles both, but the path is slightly different for each — explained below.

---

## The Building Blocks

Think of the system as having four layers:

```
┌─────────────────────────────────────────────────┐
│  YOUR EXISTING WORKFLOWS                        │
│  (Aimfox Reply Agent, Outcraft Reply Agent)     │
│  These handle the actual conversations          │
└─────────────────────────────────────────────────┘
                      ↕
┌─────────────────────────────────────────────────┐
│  MCO WORKFLOWS                                  │
│  The connective tissue — log, recall, follow up │
└─────────────────────────────────────────────────┘
                      ↕
┌─────────────────────────────────────────────────┐
│  SUPABASE (The Database)                        │
│  The shared memory — stores everything          │
└─────────────────────────────────────────────────┘
                      ↕
┌─────────────────────────────────────────────────┐
│  DELIVERY PLATFORMS                             │
│  Gmail · Aimfox · Monday.com                   │
└─────────────────────────────────────────────────┘
```

---

## Layer 1: Your Existing Workflows

These are the workflows you already built and use daily. We have not touched them.

### Aimfox Nextus AI Reply Agent
Handles all LinkedIn conversations. When a lead replies on LinkedIn, this workflow reads the message, decides what to say, and sends a reply — all automatically.

### Outcraft Reply Agent
Handles incoming email replies. When someone replies to a cold outreach email via ByZone, this workflow reads it and sends an AI-written response.

### Outcraft Followup Agent
Handles email follow-ups. If a lead showed interest but went quiet, this workflow sends a follow-up email after a set number of days.

**What you will add to these** (a few small nodes — instructions provided separately):
- A call to log the conversation to the shared database
- A call to fetch the lead's full history before the AI replies

---

## Layer 2: The MCO Workflows

These are the five workflows we built. Each has one specific job.

---

### 1. Write Conversation Event
**Job:** Log every message to the shared database.

Every time a lead sends or receives a message on any channel, this workflow is called. It records:
- Who the lead is
- What channel (LinkedIn, email, phone, SMS)
- What was said
- What their intent seems to be (interested, not interested, wants to book, etc.)
- When it happened

It also updates Monday.com automatically so your pipeline stays current.

Think of it as the person at the reception desk writing notes after every call or meeting.

**It also decides:** If a lead shows strong interest on one channel, it schedules a follow-up on a different channel. For example — if someone says "I'm interested" on LinkedIn, it schedules an email follow-up.

---

### 2. Fetch Cross-Channel Context
**Job:** Pull up a lead's full history before anyone replies to them.

Before your AI agent writes a reply on any channel, it calls this workflow first. This workflow looks up everything that has ever happened with that lead — every email, every LinkedIn message, every call — and hands it back as a summary.

The AI agent uses that summary to write a reply that feels informed and personal, not generic.

Think of it as briefing your salesperson before they pick up the phone: "Here's everything we know about this person so far."

---

### 3. FollowUp Queue Dispatcher
**Job:** Check every 15 minutes if any follow-ups are due and trigger them.

When Write Conversation Event schedules a follow-up (say, email in 3 days), it adds it to a waiting list in the database. The Dispatcher checks that list every 15 minutes.

When a follow-up is due, the Dispatcher picks it up and hands it to the Coordinator to handle.

Think of it as an alarm clock that fires reminders when scheduled follow-ups are ready to go.

**Why not just trigger it immediately?**
Because follow-ups are time-delayed by design. You don't want to email someone the same second they message you on LinkedIn. The queue lets you schedule "send this in 3 days" reliably — and it survives crashes, restarts, and outages because the schedule lives in the database, not in memory.

---

### 4. Centralized Follow-Up Coordinator
**Job:** Write and send cross-channel follow-up messages.

When the Dispatcher says "this follow-up is due," the Coordinator takes over. It:

1. Fetches the lead's full conversation history from the database
2. Sends that history to Claude (AI) with a channel-specific prompt
3. Claude writes a follow-up message tuned for that channel
4. The message is sent via the right platform:
   - **Email** → sent via Gmail (anik@nextus.ai)
   - **LinkedIn** → sent via Aimfox
5. The sent message is logged back to the database
6. The queue row is marked as done

**Why different prompts per channel?**
A LinkedIn message and an email read completely differently. LinkedIn should be short, casual, conversational — 80 words max. Email can be a bit longer and more structured. Using the same prompt for both would produce mediocre results on both. Each channel has its own Claude prompt tuned for that format.

---

### 5. Connection Accepted Handler
**Job:** Send a personalized first message the moment a lead accepts a LinkedIn connection request.

This is specific to email-sourced leads that were added to an Aimfox LinkedIn campaign.

When a lead accepts the connection request:
1. Aimfox fires a signal to this workflow immediately
2. The workflow fetches the lead's full email history from the database
3. Claude writes a warm, context-aware first LinkedIn message
4. The message is sent right away via Aimfox
5. Everything is logged back to the database

Think of it as someone accepting your business card, and you immediately following up with a personal note referencing the conversation you already had with them over email.

---

## Layer 3: Supabase (The Database)

Supabase is where all the shared memory lives. It has four tables:

### leads
One row per lead. Stores everything we know about them:
- Email address (the universal ID — every lead has one)
- Full name, company
- LinkedIn profile URL
- Phone number
- Which channel they came from first
- Their overall intent (interested, booked, not interested, etc.)
- Monday.com item ID

### conversations
One row per message. Every single message sent or received on any channel gets a row here:
- Which lead it belongs to
- Which channel (email, LinkedIn, voice, SMS)
- Whether it was inbound or outbound
- The message content
- The intent detected in that message
- When it happened

This is what Fetch Context reads when building the history summary.

### follow_up_queue
The waiting list. Every scheduled follow-up gets a row here:
- Which lead
- Which channel to follow up on
- When to send it
- Status (pending, sent, skipped, failed)

The Dispatcher reads this table every 15 minutes.

### phone_map
Maps phone numbers to email addresses. Used for voice and SMS channels — since those only know a phone number, this table is how we find the right lead in the database.

---

## Layer 4: Delivery Platforms

### Gmail (anik@nextus.ai)
Used for all warm follow-up emails. This is your personal/work email, not a cold outreach inbox. When the Coordinator sends an email follow-up, it comes from here.

### Aimfox API
Used for all LinkedIn messages. Aimfox is the bridge between our automation and LinkedIn — since LinkedIn has no public messaging API, Aimfox handles sending and receiving on our behalf.

### Monday.com
Used for pipeline tracking only. Every time a conversation is logged, the lead's Monday.com item is updated with the latest message and intent. This keeps your pipeline visible without any manual updates.

---

## The Two Full Flows

### Flow A: Lead comes from LinkedIn

```
Lead replies on LinkedIn
        ↓
Aimfox Nextus AI Reply Agent handles it
        ↓
[you add] → Fetch full history from database
        ↓
AI writes an informed reply → sent on LinkedIn
        ↓
[you add] → Log this conversation to database
        ↓
If lead says "I'm interested":
  → Schedule an email follow-up in X days
        ↓
X days later → Queue Dispatcher picks it up
        ↓
Follow-Up Coordinator:
  - Fetches full LinkedIn + any other history
  - Claude writes a warm email (email prompt)
  - Sends via Gmail (anik@nextus.ai)
  - Logs sent email to database
```

---

### Flow B: Lead comes from Email (ByZone)

```
Lead replies to cold email
        ↓
Outcraft Reply Agent handles it
        ↓
[you add] → Log this conversation to database
        ↓
Lead shows interest (LEAD_INTERESTED event)
        ↓
[you add two nodes]:
  1. Log to database (Write Event)
  2. Add lead to Aimfox LinkedIn campaign
        ↓
Aimfox sends connection request on LinkedIn
        ↓
Lead accepts the connection
        ↓
Connection Accepted Handler fires:
  - Fetches full email history from database
  - Claude writes a warm LinkedIn first message
    (referencing what was discussed over email)
  - Sends via Aimfox immediately
  - Logs to database
```

---

## What Still Needs to Happen

| What | Who does it | Status |
|---|---|---|
| Create Aimfox "MCO LinkedIn Follow-Up" campaign | You | Pending |
| Add campaign ID to Coordinator workflow | Me (once you share ID) | Pending |
| Link Gmail credential in Coordinator workflow | You (inside n8n) | Pending |
| Link Anthropic credential in Coordinator workflow | You (inside n8n) | Pending |
| Add 2 nodes to Outcraft Followup Agent | You (instructions provided) | Pending |
| Add Write Event + Fetch Context calls to Aimfox Reply Agent | You (instructions to be provided) | Pending |
| Add Write Event + Fetch Context calls to Outcraft Reply Agent | You (instructions to be provided) | Pending |
| Build Retool dashboard (lead list + conversation timeline) | Me | Not started |
| Retell AI integration (voice/SMS) | Me (waiting for workflow JSONs) | Not started |

---

## The One Thing to Remember

Every channel in this system feeds into and reads from the same database. That is the entire point.

It does not matter if a lead contacted you on LinkedIn first and then replied to an email a week later. When your AI agent goes to reply to that email, it already knows about the LinkedIn conversation. It replies like someone who has been paying attention — because the system has been.

That is what makes the outreach feel human.
