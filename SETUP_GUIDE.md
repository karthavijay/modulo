# Modulo on Render: setup guide

This guide is for you, not for a developer. No terminal and no commands. You will click through a few websites and paste in three keys. It takes about 30 minutes, once.

## What you get

When it is set up, it all runs by itself:

1. A client chats with the agent team on your website and presses **Send brief**.
2. Iris writes the course design. A few minutes later you get an email with the design and two buttons: **Approve** and **Request changes**.
3. If you ask for changes, type them in plain words. Iris revises the design and emails you again.
4. When you press **Approve**, Theo writes the storyboard and Quinn checks it. This takes about an hour.
5. You get a second email with the **Word storyboard** and the **QA report** attached.

6. **Optional:** the storyboard email also offers the **production pack**. If you press it, the other seven team members each write their document (listed below). That takes about 20 to 30 minutes, costs about $4 to $6, and arrives as one Word file.

7. **Optional: build the real course.** On the course page, press **Have Dev build the course**. Dev turns the storyboard into a course that plays in any browser, with pages, knowledge checks, a scored assessment with results and retake, and progress tracking. It takes about 10 to 20 minutes and costs about $2 to $4. Or press **Make a free quick draft** to copy the storyboard in as it is, instantly and for free. You then edit it in the **course editor** and download it for your LMS (see "The course editor" below).

Nothing expensive happens until you approve. The storyboard step is the part that costs about $7, and the production pack only runs when you ask for it.

## The team and what each one makes

| Team member | When | What they make |
|---|---|---|
| Iris, Instructional Designer | First | The course design: objectives, modules, assessment |
| Theo, Storyboard Writer | After you approve the design | The screen-by-screen storyboard with narration |
| Quinn, QA Reviewer | With Theo | The QA report, and Theo fixes what Quinn finds |
| Sam, SME Liaison | Production pack | SME review pack: facts to confirm, interview questions, sign-off checklist |
| Vera, Visual Designer | Production pack | Visual style guide, asset list with alt text, and ready-to-use image prompts |
| Mila, Voiceover and Media Producer | Production pack | Voice casting, recording specs, pronunciation guide, caption plan |
| Dev, eLearning Developer | Production pack | Build specification for Storyline or Rise: triggers, variables, accessibility |
| Lars, LMS and SCORM Administrator | Production pack | Publishing settings, tracking tests and LMS launch checklist |
| Nia, Learning Analyst | Production pack | How to measure whether the course worked, with a survey and 90-day plan |
| Paige, Project Manager | Production pack, last | Project plan, schedule, open items and risks, ready for the client |

The production pack is documents only. No voice is recorded, no images are generated and nothing is uploaded to an LMS. Those could be connected later (for example ElevenLabs for voice, an image service for Vera's prompts, SCORM Cloud for Lars) once you decide you want them.

Everything (website, chat and the course team) lives in one place, called Render. You no longer need Netlify.

## What it costs

| What | Cost |
|---|---|
| Render (keeps the website and the team running all day) | about $7 a month, plus about $0.25 a month for storage |
| Claude (the AI) | pay as you go: a few cents per website chat, about $7 per finished storyboard |
| Resend (sends the emails) | free for up to 3,000 emails a month |
| GitHub (stores the files Render reads) | free |

## Step 1. Get your Claude key (5 minutes)

You may already have one from the website work. If so, skip to Step 2.

1. Go to **platform.claude.com** and sign in.
2. Open **Billing**, add a card and buy some credit (for example $25).
3. Still in Billing, set a **monthly spend limit**, for example $100. This is your safety net.
4. Open **API Keys**, press **Create Key**, name it "Modulo" and copy the key. It starts with `sk-ant-`. Keep it in a safe note for Step 4.

## Step 2. Set up email with Resend (5 minutes)

1. Go to **resend.com** and sign up. **Use the email address where you want to receive the approval emails.** This matters: until you connect your own company domain, Resend only lets the service email the address you signed up with.
2. Open **API Keys**, press **Create API Key**, name it "Modulo", keep "Full access" or "Sending access", and copy the key. It starts with `re_`. Keep it for Step 4.

## Step 3. Put the files on GitHub (10 minutes)

Render picks up its files from GitHub, a free website for storing projects.

1. Go to **github.com** and sign up (or sign in).
2. Press the **+** at the top right, then **New repository**. Name it `modulo`, choose **Private**, and press **Create repository**.
3. Now fill it with the files. You have two choices:
   - **Easiest:** tell Claude in this project "my GitHub repository is called modulo" and Claude will put the files in for you. (You may be asked to connect GitHub to Claude once.)
   - **By hand:** unzip `modulo-render.zip` on your computer. On the new repository page, click **uploading an existing file**. Open the unzipped `modulo-render` folder, select **everything inside it** (not the folder itself) and drag it onto the page. Wait until every file shows, then press **Commit changes**.

When it is done, the repository page should list files like `render.yaml`, `app.py` and folders `pipeline` and `public`.

## Step 4. Create the service on Render (10 minutes)

1. Go to **render.com** and press **Get Started**. Choose **Sign up with GitHub**, so Render can see your repository.
2. In the Render dashboard, press **New** (top right), then **Blueprint**.
3. Pick the `modulo` repository. (If you don't see it, press the link to give Render access to it.)
4. Render reads the setup file and shows one service called **modulo**. It asks you for three values. Paste them:
   - **ANTHROPIC_API_KEY**: your Claude key from Step 1
   - **RESEND_API_KEY**: your Resend key from Step 2
   - **NOTIFY_EMAIL**: the email you signed up to Resend with
5. Press **Apply** (or **Deploy Blueprint**). Render may ask for a card for the $7 plan.
6. Wait about 5 minutes. When the service shows **Live**, you will get an email titled **"Your Modulo service is live"** with a link to your private **dashboard**. Bookmark it. The dashboard lists every course and where it is.

Your website is now live at an address like `https://modulo-xxxx.onrender.com`. You'll find it at the top of the service page in Render.

## Step 5. Try it

You can try it straight away from the dashboard, without going through the website chat:

1. Open the dashboard link from the welcome email.
2. Under **Start a course from a brief you received elsewhere**, type a client name and email, paste a brief (or the sample forklift brief from the `pipeline/samples` folder) and press **Start**.
3. Within a few minutes you get the design email. This step costs well under a dollar.
4. Press **Request changes** to see how a revision works, or **Approve** to run the full storyboard (about an hour, about $7).

Or do the real thing: open your website, press **Start a course**, chat with the team, and press **Send brief to Modulo**.

**Want to test without spending anything?** In Render, open the service, then **Environment**, add `MODULO_FAKE` with the value `1` and save. The team then uses the finished forklift sample instead of calling Claude, and a whole run takes a few seconds. Delete `MODULO_FAKE` again to go live.

## Using your own web address

In Render, open the service, then **Settings**, **Custom Domains**, **Add Custom Domain**, and type your domain (for example `www.modulo-learning.com`). Render shows one or two records to add where you bought the domain. HTTPS is added automatically. Then, under **Environment**, add `PUBLIC_URL` with your full address (for example `https://www.modulo-learning.com`) so the links in your emails use it.

To have emails come from your own domain instead of Resend's test address, add the domain in Resend under **Domains** and follow the steps there. Then you can also send to other team members. Tell Claude when you've done this and it can switch the sender for you.

## Day to day

- **You only need your inbox.** Each email has the buttons you need.
- **The dashboard** shows every course, its status and cost, and lets you start a course from a brief that came in by email or phone.
- **Talking to one agent:** on the dashboard, press **Talk to the team** (or the link next to a course). Pick the course, then pick any of the ten team members, for example **Iris** (design), **Theo** (storyboard), **Quinn** (QA) or **Vera** (visuals) and chat as you would with a colleague. For example: "Theo, make screen 2.3 shorter and add a knowledge check after it", or "Quinn, why did you flag screen 4.1?". When a team member suggests a change to their work, a box appears with **Save to course**. Nothing changes until you press it. Saving keeps a backup of the old version and rebuilds the Word storyboard, which you download from the course page as usual. You can chat at any stage, even while a course is in production: the agents see the brief, the design and the latest draft. Changes can only be saved once the current step has finished. No course yet? Press **Add the sample forklift course** on the dashboard for a free, finished demo course to chat about. Starting a chat costs about 20 cents, because the agent reads the whole course; follow-up messages cost a few cents.
- **Seeing every file of a course:** on the dashboard, press **Course files** next to a course (it's also at the top of each course page). It lists everything saved for that course with a short explanation of each file: the brief, Iris's design, each storyboard draft, each QA round, costs, and backups. Press **Open** to read a file in the browser, or **Download** to save it, including the Word storyboard.
- **The course editor** (your own SCORM authoring tool): open it from the course page or the dashboard (**Course editor**). On the left is every page of the course; click one to change it. Each page is made of blocks: text, headings, pictures, callout boxes, click-to-reveal panels, questions and the results block. Add, move or delete blocks with the buttons on each one. Changes save by themselves. **Preview** opens the course as a learner sees it. **Course settings** has the pass mark, number of tries, whether pages unlock in order, and the main color. **Check and download** lists anything to fix (for example a question with no right answer) and gives you three downloads: **SCORM 1.2** (works in almost every LMS; pick this if unsure), **SCORM 2004**, and **Website** (no LMS). It also has a preview link you can send to a client, and **Go back to the previous save** if you change your mind. Pictures start as "Image to come" placeholders until you upload your own. To test a SCORM file before it goes to a client, upload it to a free SCORM Cloud account (cloud.scorm.com) and launch it there.
- **Changing how an agent works:** the agents' job descriptions are the files in `pipeline/agents/` on GitHub. Click a file, press the pencil icon, edit the text, and press **Commit changes**. Render picks up the change by itself within a few minutes.
- **Safety limits:** the service starts at most 10 new briefs a day (you can change `MAX_BRIEFS_PER_DAY` in Render, Environment), and your Claude spend limit caps the total.

## If something goes wrong

- **No emails arrive.** Check your spam folder first. Then check that `NOTIFY_EMAIL` in Render (service, Environment) is exactly the address you used to sign up to Resend. The dashboard shows a red note if email is not set up.
- **An email says "Problem with the course".** Usually the Claude account ran out of credit, hit its spend limit, or the key is wrong. Fix it at platform.claude.com, then press **Try again** on the course page. Finished steps are kept, so you never pay twice.
- **The service restarted in the middle of a storyboard.** Nothing to do. It picks up where it stopped.
- **Render says the service ran out of memory.** In Render, open the service, **Settings**, **Instance Type**, and choose **Standard** (about $25 a month).
- **Anything else:** in Render, open the service and click **Logs**. Copy the last red lines and paste them to Claude.

## Keep in mind

- Every storyboard is a draft for human review. Agents mark facts they could not confirm as `[CLIENT TO CONFIRM: ...]`, but your subject expert must still check the content, especially for safety topics.
- Client briefs are sent to Claude. Check that this fits your client agreements, and add a privacy notice to the website before launch.
