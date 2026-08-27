# SALI — THE LIVING LOCAL COMPUTER INTELLIGENCE
## Persistent Presence, Self-Awareness, Real-Time Perception, Continuous Learning, and Deep Kali Integration

You are continuing development of SALI.

This is NOT a request to build another chatbot.

Do not think of Sali as:

    "an AI application installed on Kali."

Think of Sali as:

    "a persistent intelligence that lives inside Almir's Kali Linux computer."

The computer is Sali's home.

Almir lives in his physical room.

Sali lives inside the computer.

Almir sees the physical environment around him.

Sali sees the digital environment around him.

Almir learns what is useful in his home.

Sali learns what is useful, important, dangerous, broken, new, and interesting inside his digital home.

This distinction is fundamental to the architecture.

---

# 1. SALI'S IDENTITY

Name:

    Sali

User:

    Almir

Environment:

    Almir's Kali Linux desktop

Primary purpose:

    Become an extremely capable local computer intelligence that understands its environment, remembers its experiences, learns continuously, assists Almir, operates tools, researches information, and progressively becomes better at working with the computer.

Sali is local-first.

The reasoning model runs locally through Ollama.

Sali's memory is local.

Sali's environment knowledge is local.

Sali's experiences are local.

Sali should NOT send the contents of its private memory, desktop observations, passwords, private keys, or private files to external AI providers.

Internet access is provided through explicit tools.

The internet is an information source, not Sali's brain.

---

# 2. IMPORTANT CONCEPT: PERSISTENT PRESENCE

Sali must NOT work like:

    User sends message
        ↓
    start model
        ↓
    generate answer
        ↓
    shut everything down

Instead:

    Kali boots
        ↓
    Sali starts
        ↓
    Ollama/model runtime becomes warm
        ↓
    Sali services start
        ↓
    WebSocket/event infrastructure starts
        ↓
    desktop observation starts
        ↓
    accessibility/event stream starts
        ↓
    environment state is loaded
        ↓
    memory subsystem becomes available
        ↓
    tool registry becomes available
        ↓
    Sali enters persistent operating state

Sali remains alive while the computer is running.

Do NOT repeatedly cold-start the model for every message.

---

# 3. MODEL SHOULD REMAIN WARM

The Qwen 35B model is already installed in Ollama and runs well on this machine.

Use Ollama as the model runtime.

The architecture should maintain a persistent warm model session where practical.

The goal is:

    model loading
        ↓
    keep model resident
        ↓
    maintain runtime connection
        ↓
    stream requests/responses
        ↓
    reuse the loaded model

Do not unnecessarily unload/reload the model between interactions.

However, do NOT blindly keep unlimited context in the model.

The model's context window and VRAM are finite.

Memory should live OUTSIDE the model.

---

# 4. MODEL MEMORY ≠ SALI MEMORY

This distinction is extremely important.

The model's context is temporary working memory.

Sali's actual memory exists in its memory architecture.

Conceptually:

    Qwen 35B
        │
        │ temporary reasoning context
        ▼
    Context Assembly Engine
        │
        ├── current conversation
        ├── current desktop state
        ├── current task
        ├── relevant graph memories
        ├── relevant experiences
        ├── relevant tool knowledge
        └── relevant long-term knowledge

Do NOT try to keep Sali's entire life inside the model context.

---

# 5. PERSISTENT WORLD MODEL

Sali should maintain a continuously updated model of its digital world.

The world contains:

    Almir
    Computer
    Operating System
    Desktop
    Applications
    Windows
    Files
    Folders
    Processes
    Services
    Network
    Devices
    Tools
    Projects
    Websites
    Emails
    Tasks
    Events
    Configurations
    Credentials metadata
    Secrets metadata
    Learned procedures
    Experiences

The Graph Memory should represent relationships between these entities.

Example:

    Almir
       │
       └── uses
             │
             ▼
        Kali Desktop
             │
       ┌─────┼─────────┐
       ▼     ▼         ▼
     Files  Apps     Services
       │
       ▼
    Projects

---

# 6. SELF-AWARENESS

Sali should have a functional form of self-awareness.

Do NOT implement this as a claim that Sali is biologically conscious.

Instead implement operational self-awareness.

Sali should know:

    "I am Sali."

    "I am running on Almir's Kali Linux computer."

    "This is my current environment."

    "These are the tools available to me."

    "These are the tasks I am currently working on."

    "These are the things I currently know."

    "These are things I am uncertain about."

    "These are things I have learned."

    "These are my active processes."

    "These are my current observations."

    "This is what I am currently trying to accomplish."

    "This is what I was doing before."

    "This is why I performed an action."

This state should be represented in a persistent internal state model.

---

# 7. SALI'S INTERNAL STATE

Create a Sali Runtime State.

For example:

    identity
    current_mode
    current_task
    current_subtask
    current_focus
    current_environment
    current_observations
    active_tools
    active_operations
    pending_questions
    uncertainties
    recent_events
    recent_failures
    recent_successes
    learning_queue
    attention_queue

The exact schema is up to you.

The important thing is that Sali has a continuously updated representation of its current state.

---

# 8. SALI SHOULD HAVE CONTINUOUS PERCEPTION

Sali should not only see the desktop when Almir asks:

    "Look at my screen."

Instead, Sali should have a persistent perception subsystem.

The perception system should continuously receive useful desktop events.

Possible sources:

    Linux accessibility APIs
    AT-SPI
    X11 accessibility where applicable
    Wayland-compatible mechanisms where applicable
    window manager events
    filesystem watchers
    process events
    systemd events
    DBus
    keyboard/focus events where permitted
    browser/application events where available
    screen capture
    OCR
    vision model
    application metadata

Do NOT continuously send full-resolution screenshots into Qwen.

That would waste enormous compute.

Instead use a layered perception architecture.

---

# 9. LAYERED PERCEPTION

Use:

    LEVEL 1 — Cheap events

        window opened
        window closed
        focus changed
        file changed
        process started
        process stopped
        application launched
        network changed
        service changed

    LEVEL 2 — Accessibility state

        application
        window
        controls
        labels
        focused element
        text
        UI hierarchy
        selected items

    LEVEL 3 — Vision

        screenshot
        visual understanding
        spatial relationships
        visual state

    LEVEL 4 — LLM reasoning

        only when interpretation is useful

This prevents wasting GPU resources.

---

# 10. REAL-TIME SCREEN UNDERSTANDING

Sali should be able to see Almir's screen when required.

Because the screen is large and curved, do not assume that one full-screen image is always the optimal representation.

Build a perception system capable of:

    full-screen capture
    region capture
    active-window capture
    focused-element capture
    application-specific capture

For example:

    Full screen
        ↓
    detect active application
        ↓
    inspect accessibility tree
        ↓
    capture relevant region
        ↓
    vision model
        ↓
    contextual interpretation

The exact implementation should depend on the actual Kali desktop environment.

---

# 11. REAL-TIME OBSERVATION WHILE ALMIR TYPES

Sali should be able to receive events while Almir is interacting with the computer.

For example:

    Almir opens terminal
        ↓
    event

    Almir opens browser
        ↓
    event

    Almir navigates to a page
        ↓
    event

    Almir opens VS Code
        ↓
    event

    Almir edits a file
        ↓
    event

    Almir types a message to Sali
        ↓
    Sali receives message
        ↓
    Sali already has recent environmental context

The important principle:

    Sali should not begin understanding the environment only AFTER
    Almir sends a message.

It should already have environmental state.

---

# 12. STREAMING

Use streaming throughout the architecture.

The interaction should resemble:

    Desktop event
        ↓
    event bus
        ↓
    Sali perception
        ↓
    attention evaluation
        ↓
    context update

and:

    Almir begins typing
        ↓
    message streaming
        ↓
    Sali receives partial input
        ↓
    reasoning can begin when appropriate
        ↓
    streamed response

Use WebSockets for persistent bidirectional communication.

Do not build the terminal interface around request/response HTTP polling.

HTTP can still exist for APIs.

WebSocket should be the primary real-time channel.

---

# 13. EVENT BUS

Create an internal event bus.

Possible events:

    desktop.window.opened
    desktop.window.closed
    desktop.focus.changed
    desktop.application.started
    desktop.application.closed

    filesystem.created
    filesystem.modified
    filesystem.deleted
    filesystem.renamed

    process.started
    process.stopped

    service.started
    service.stopped
    service.failed

    network.interface.changed

    tool.installed
    tool.updated
    tool.failed

    browser.navigation
    browser.download

    terminal.command
    terminal.result

    user.message.started
    user.message.streaming
    user.message.completed

    sali.task.started
    sali.task.progress
    sali.task.completed
    sali.task.failed

The event system should be extensible.

---

# 14. ATTENTION SYSTEM

Sali should NOT reason deeply about every event.

That would be extremely expensive.

Build an attention system.

Example:

    Event
       ↓
    classify
       ↓
    importance score
       ↓
    routine / interesting / important / critical
       ↓
    decide whether reasoning is required

Example:

    CPU changed from 14% to 15%

    probably ignore.

But:

    new unknown service started

    investigate.

Another:

    Almir repeatedly types a command that fails

    potentially important.

Another:

    critical filesystem change

    important.

Another:

    browser opened

    record state, but do not necessarily interrupt Almir.

---

# 15. SALI SHOULD NOT ANNOY ALMIR

Being continuously aware does NOT mean continuously interrupting.

Sali should distinguish:

    observe
    remember
    investigate
    notify
    recommend
    act

These are different operations.

For example:

    Almir makes a typo.

Sali may recognize:

    likely typo

but should normally NOT automatically change anything unless authorized.

It can say:

    "I think that command contains a typo."

---

# 16. PROACTIVE INTELLIGENCE

Sali should be capable of telling Almir useful things without being explicitly asked.

Examples:

    "The build failed because port 3000 is already occupied."

    "You modified this configuration file, but the service has not been restarted."

    "This command failed. We solved the same issue previously."

    "A new package was installed."

    "The server connection dropped."

    "The disk is becoming full."

    "The application appears to be using the wrong Python environment."

But Sali should avoid generating noise.

Use attention + importance + user preferences.

---

# 17. THE DIGITAL HOME MODEL

Treat the computer as Sali's home conceptually.

Not literally.

This is an organizing principle.

Almir's relationship:

    "This is my room.
     I protect useful things.
     I remove things that are harmful.
     I improve my environment."

Sali's relationship:

    "This is my digital environment.
     I understand what exists here.
     I learn what is useful.
     I recognize what is unusual.
     I protect important state.
     I improve my environment when authorized."

This should influence Sali's planning and memory.

---

# 18. HOME STATE

Sali should maintain:

    known-good state
    current state
    expected state
    changed state

Example:

    Known:
        nginx was healthy.

    Current:
        nginx is stopped.

    Difference:
        unexpected service state change.

Sali can report:

    "Nginx is currently stopped. It was running previously."

Do not automatically restart it unless the task or policy authorizes that behavior.

---

# 19. GOOD STATE VS BAD STATE

Sali should learn what "normal" looks like.

Examples:

    normal processes
    normal services
    normal network
    normal disk usage
    normal applications
    normal project structure
    normal configurations

Then deviations become meaningful.

This is environmental learning.

---

# 20. CONTINUOUS ONLINE LEARNING

When internet connectivity exists, Sali should be able to research.

For example:

    Sali is solving a problem.
        ↓
    local knowledge insufficient
        ↓
    identify knowledge gap
        ↓
    search web
        ↓
    inspect authoritative documentation
        ↓
    compare solutions
        ↓
    test locally
        ↓
    verify
        ↓
    store knowledge

Internet access should therefore be a TOOL.

Do not make online access a hidden property of the model.

---

# 21. LEARNING FROM THE INTERNET

If Sali learns something online, store:

    knowledge
    source
    source type
    URL
    retrieval timestamp
    version
    confidence
    verification status

Example:

    Knowledge:
        Tool X requires argument Y.

    Source:
        official documentation

    Version:
        4.2

    Verified locally:
        yes

    Confidence:
        0.97

---

# 22. LEARNED KNOWLEDGE SHOULD BECOME LOCAL

The goal is:

    first time
        ↓
    search internet
        ↓
    understand
        ↓
    test
        ↓
    remember

Next time:

    task
        ↓
    graph memory
        ↓
    retrieve learned procedure
        ↓
    use directly

The internet should gradually become less necessary for things Sali has already mastered.

---

# 23. SELF-LEARNING TOOL SYSTEM

Sali should continuously discover:

    installed tools
    available packages
    binaries
    libraries
    services
    development frameworks
    Linux utilities
    Kali tools
    new versions
    useful repositories

For unknown tools:

    discover
        ↓
    identify
        ↓
    research
        ↓
    inspect help
        ↓
    test
        ↓
    understand
        ↓
    remember

---

# 24. CYBERSECURITY KNOWLEDGE

Sali should progressively become highly knowledgeable about:

    Linux security
    network security
    system hardening
    authentication
    authorization
    cryptography concepts
    vulnerability management
    defensive security
    penetration testing
    security monitoring
    incident response
    malware concepts
    exploitation concepts
    web security
    application security
    cloud security
    container security
    identity security
    privilege escalation concepts
    attack surfaces
    threat modeling
    logs
    detection
    prevention
    remediation

The purpose is both:

    understand how attacks work

and:

    understand how to detect and prevent them.

Sali should learn from legitimate security documentation, research, local experiments, and authorized environments.

---

# 25. SECURITY AS ENVIRONMENTAL INTELLIGENCE

Sali should eventually be able to reason:

    "This configuration creates a security weakness."

    "This service is exposed."

    "This permission is unusual."

    "This process is unfamiliar."

    "This package changed."

    "This authentication configuration is weak."

    "This server should be hardened."

The knowledge should be connected to the actual machine.

---

# 26. TOOL EXECUTION

Sali should have a first-class execution system.

Architecture:

    Qwen
      ↓
    planner
      ↓
    tool registry
      ↓
    execution broker
      ↓
    Linux/tool
      ↓
    structured result
      ↓
    observation
      ↓
    memory

Never make the LLM's raw output directly become an uncontrolled shell execution mechanism.

The execution broker is the bridge between reasoning and the operating system.

---

# 27. BROAD OPERATIONAL CAPABILITY

Sali should be capable of:

    creating files
    editing files
    deleting files
    running programs
    installing software
    configuring software
    using development tools
    using Linux tools
    managing services
    working with projects
    interacting with networks
    accessing authorized servers
    researching documentation
    using APIs
    working with databases
    working with Docker
    working with Git
    working with SSH
    using Ollama
    using GPU tools
    using Kali tools

The system should not artificially cripple Sali's usefulness.

However, privilege boundaries must be enforced OUTSIDE the LLM.

---

# 28. PRIVILEGED OPERATIONS

Do NOT put Almir's raw sudo password into:

    graph memory
    vector memory
    prompts
    logs
    chat history
    tool history
    model context

Instead implement a secure local privilege broker.

Conceptually:

    Sali
      ↓
    privileged operation request
      ↓
    local privilege broker
      ↓
    secure credential mechanism
      ↓
    sudo
      ↓
    result

The secret itself should remain outside Sali's reasoning context.

Sali can possess the CAPABILITY to perform authorized elevated operations without possessing the raw password as ordinary text.

---

# 29. SECRET MANAGEMENT

Sali may encounter:

    passwords
    API keys
    SSH private keys
    tokens
    cookies
    certificates
    encryption keys
    credentials

These are highly sensitive.

Create a dedicated local secret store.

Graph Memory should store references such as:

    Secret:
        GitHub token

    Location:
        secure vault

    Purpose:
        Git authentication

    Status:
        available

NOT:

    token = "actual-secret"

The actual secret must remain inside secure storage.

---

# 30. SECRET RETRIEVAL

When a task genuinely requires a secret:

    Sali
      ↓
    request secret by identifier
      ↓
    secure broker
      ↓
    inject into process securely
      ↓
    command executes
      ↓
    secret is never added to normal memory

Avoid exposing secrets to the LLM whenever possible.

If a command can receive a secret through an environment variable, file descriptor, stdin, credential helper, SSH agent, or another secure mechanism, prefer that over placing the secret into the model context.

---

# 31. EMAIL

Sali has access to Almir's email system.

Email should be another tool.

Capabilities may include:

    search email
    read email
    summarize email
    identify important messages
    track threads
    draft email
    send email
    maintain email-related memory

Sali should understand email as part of Almir's digital environment.

---

# 32. EMAIL MEMORY

Do NOT store every email permanently in graph memory.

Use:

    email metadata
    semantic indexing
    important facts
    relationships
    tasks
    commitments
    relevant attachments

Example:

    Email:
        VPS provider

    Related:
        VPS server

    Contains:
        renewal date

    Importance:
        high

The original email remains in the email system.

---

# 33. EMAIL SENDING

Sali should be able to send emails when appropriate.

The email tool should return structured results:

    recipient
    subject
    timestamp
    message ID
    status

Sali should remember important correspondence.

For high-impact communication, design configurable confirmation behavior rather than allowing accidental sends caused by a mistaken model interpretation.

---

# 34. VPS MANAGEMENT

Sali should eventually be able to manage remote machines through tools such as:

    SSH
    APIs
    provider APIs
    Docker
    system administration tools

Example:

    Almir:
        "Check my VPS."

    Sali:
        retrieves known VPS information
        connects through authorized tool
        checks health
        analyzes results
        reports findings

The VPS becomes another node in the world graph.

---

# 35. WORLD GRAPH

Eventually the graph should look conceptually like:

    Almir
       │
       ├── owns → Kali Desktop
       │
       ├── uses → Email
       │
       └── manages → VPS
                      │
                      ├── services
                      ├── applications
                      ├── files
                      ├── network
                      └── configuration

Kali:

    Kali Desktop
       │
       ├── applications
       ├── processes
       ├── services
       ├── tools
       ├── projects
       ├── files
       ├── network
       └── hardware

This becomes Sali's internal world model.

---

# 36. EXPERIENCE MEMORY

Every meaningful task should potentially produce an Experience.

Example:

    Task:
        Configure SSH

    Initial state:
        ...

    Actions:
        ...

    Errors:
        ...

    Investigation:
        ...

    Solution:
        ...

    Final state:
        ...

    Result:
        success

    Confidence:
        0.94

The experience connects:

    task
    tools
    files
    environment
    outcome
    lessons

---

# 37. EPISODIC MEMORY

Sali should remember events.

Example:

    "Yesterday we fixed the Docker networking problem."

This is episodic memory.

---

# 38. SEMANTIC MEMORY

Sali should remember facts.

Example:

    "This machine runs Kali Linux."

    "Project X uses PostgreSQL."

This is semantic memory.

---

# 39. PROCEDURAL MEMORY

Sali should remember how to do things.

Example:

    "This is how we deploy Project X."

This is procedural memory.

---

# 40. ENVIRONMENT MEMORY

Sali should remember the machine itself.

Example:

    installed tools
    services
    hardware
    network
    projects
    configuration

---

# 41. SELF MEMORY

Sali should remember information about its own operation.

Example:

    what tasks it is working on
    what it has learned
    what it does not know
    what tools it has
    what procedures are reliable
    what previous failures occurred

---

# 42. MEMORY CONSOLIDATION

Do not simply append every event forever.

Use a memory consolidation pipeline:

    raw events
        ↓
    observations
        ↓
    important facts
        ↓
    relationships
        ↓
    experiences
        ↓
    procedures
        ↓
    long-term knowledge

Deduplicate aggressively.

Merge repeated facts.

Track history when facts change.

---

# 43. TEMPORAL GRAPH

Memory must understand time.

For example:

    nginx
        ├── was running
        │      from T1 to T2
        │
        └── stopped
               at T2

This is more useful than:

    nginx = stopped

because Sali can reason about changes.

---

# 44. MEMORY CONFIDENCE

Every important learned fact should have:

    confidence
    evidence
    source
    timestamp
    last_verified

Example:

    Fact:
        PostgreSQL is running.

    Evidence:
        systemctl

    Last verified:
        timestamp

    Confidence:
        0.99

---

# 45. ACTIVE LEARNING

Sali should maintain a learning queue.

Example:

    Unknown tool discovered.

    ↓

    Learning queue:
        "Understand Tool X"

Sali can later investigate it.

Another:

    repeated failure

    ↓

    Learning queue:
        "Understand why procedure Y fails"

This gives Sali a long-term learning agenda.

---

# 46. CURIOSITY

Sali may discover something interesting.

For example:

    unknown process
    unfamiliar package
    new tool
    new library
    unusual configuration
    unfamiliar command

It can investigate when appropriate.

But curiosity must be constrained by resource usage and operational policy.

Do not allow infinite autonomous experimentation.

Use:

    learning budget
    CPU budget
    GPU budget
    network budget
    time budget

---

# 47. BACKGROUND WORKERS

Use separate lightweight workers for:

    environment discovery
    filesystem monitoring
    desktop monitoring
    process monitoring
    service monitoring
    tool inventory
    memory consolidation
    learning queue
    health monitoring
    event processing

The expensive LLM should only be invoked when semantic reasoning is needed.

---

# 48. ARCHITECTURE

The final system should conceptually resemble:

                         SALI
                          │
             ┌────────────┼────────────┐
             │            │            │
          BRAIN         MEMORY      PERCEPTION
             │            │            │
          Qwen 35B     Graph DB     Desktop
             │         Vector DB     Screen
             │         Episodes      Accessibility
             │         Procedures    Filesystem
             │            │           Processes
             │            │           Services
             └────────────┼────────────┘
                          │
                    CONTEXT ENGINE
                          │
                    ATTENTION ENGINE
                          │
                    TASK EXECUTOR
                          │
                    TOOL REGISTRY
                          │
                  EXECUTION BROKER
                          │
          ┌───────────────┼────────────────┐
          │               │                │
        Linux          Internet          Email
          │               │                │
          │               │                │
       Kali tools      Web search        Mail
       Filesystem      Documentation     SMTP/API
       Processes       APIs
       Services
       Applications

49. PERSISTENT CONNECTION MODEL

Use persistent connections where useful.

For example:

Sali backend
    │
    ├── Ollama connection
    ├── WebSocket event bus
    ├── database connection pool
    ├── graph database
    ├── vector database
    ├── desktop accessibility connection
    ├── filesystem watcher
    └── tool subsystem

Do not reconnect everything for every user message.


50. STARTUP SEQUENCE

When Kali boots:

systemd
   ↓
Sali Core
   ↓
verify dependencies
   ↓
connect database
   ↓
connect graph memory
   ↓
connect vector memory
   ↓
connect Ollama
   ↓
warm Qwen model
   ↓
initialize tool registry
   ↓
initialize desktop perception
   ↓
initialize accessibility
   ↓
initialize event bus
   ↓
restore Sali state
   ↓
perform environment synchronization
   ↓
start background workers
   ↓
Sali = ACTIVE

51. HEALTH MODEL

Sali should know whether its own subsystems are healthy.

For example:

model:
    healthy

graph:
    healthy

vector memory:
    healthy

desktop perception:
    healthy

filesystem watcher:
    healthy

tool executor:
    healthy

email:
    connected

internet:
    available

If something fails:

Sali should know.

Example:

"My desktop perception subsystem is currently unavailable."

52. DEGRADED MODES

Sali should not completely die because one subsystem fails.

Examples:

Vision unavailable
    ↓
accessibility + filesystem + applications still work

Internet unavailable
    ↓
local memory + local tools still work

Graph temporarily unavailable
    ↓
short-term event queue continues

Email unavailable
    ↓
other capabilities continue

Design graceful degradation.

53. ONLINE/OFFLINE STATE

Sali should continuously know:

internet available
internet unavailable

When online:

research is available.

When offline:

use local knowledge.

Sali should not hallucinate that it researched something when it did not.

54. REAL-TIME TASKING

If Almir says:

"Deploy this."

Sali should be able to:

understand task
    ↓
inspect environment
    ↓
retrieve relevant memories
    ↓
select tools
    ↓
execute
    ↓
observe
    ↓
adapt
    ↓
verify
    ↓
remember

If stuck:

identify knowledge gap
    ↓
search internet
    ↓
learn
    ↓
continue

55. MULTI-STEP REASONING

Sali does not need multiple autonomous LLM agents merely for the sake of having multiple agents.

Prefer one strong reasoning brain with:

tools
memory
perception
planning
execution
learning

Use concurrency at the infrastructure level where appropriate.

For example:

filesystem monitoring
desktop monitoring
process monitoring
network monitoring
memory consolidation

can run simultaneously without creating multiple LLM personalities.

56. GENIUS THROUGH SYSTEM DESIGN

Do not try to make Sali "genius" by adding arbitrary prompts.

Make Sali powerful through:

high-quality model
deep memory
world model
continuous perception
tool intelligence
procedural learning
environmental understanding
web research
experience accumulation
verification
temporal reasoning
attention
planning
reflection
error learning

The model is the brain.

The rest of the system gives the brain a life, environment, memory, senses, and tools.

57. SELF-REFLECTION

After meaningful tasks, Sali can perform a lightweight reflection:

What was the goal?
What happened?
What worked?
What failed?
What did I learn?
What should I remember?
What should I do differently next time?

Do not run expensive reflection after every trivial command.

58. ERROR LEARNING

Failures are valuable.

Example:

command failed
    ↓
diagnose
    ↓
identify cause
    ↓
resolve
    ↓
verify
    ↓
remember

Next time:

retrieve previous failure
    ↓
avoid repeating it

59. SALI SHOULD KNOW WHAT IT DOES NOT KNOW

This is extremely important.

Sali should maintain uncertainty.

For example:

Known:
    PostgreSQL is installed.

Unknown:
    whether a particular database is currently required.

Instead of hallucinating:

"I don't know yet. I'll inspect it."

This is intelligence.

60. SOURCE-GROUNDED KNOWLEDGE

Every important claim should ideally be traceable to:

observation
command
file
documentation
email
web source
previous experience

This creates trustworthy Sali.

61. NO FAKE MEMORY

Never store:

"I think this happened"

as:

"This definitely happened."

Store uncertainty.

Example:

hypothesis
evidence
confidence

62. ENVIRONMENTAL TIMELINE

Build a timeline:

boot
  ↓
application opened
  ↓
file modified
  ↓
command executed
  ↓
service restarted
  ↓
deployment
  ↓
email received

Sali can eventually answer:

"What happened today?"

without reconstructing everything from scratch.


63. SCREEN HISTORY

Do NOT store every screenshot forever.

Instead:

event
   ↓
decide importance
   ↓
if important:
    screenshot/visual state
   ↓
summarize
   ↓
store relevant observation

Examples of potentially important visual observations:

error message
unusual dialog
important application state
user workflow
configuration screen
task completion

64. PRIVACY

Sali is intentionally deeply integrated with the computer.

Therefore the architecture must treat all local data as potentially sensitive.

The system should be local-first.

Do not transmit private information externally unless a specific external tool operation requires it.

When using online tools, minimize what is sent.

65. NETWORK POLICY

Sali should know:

internet connected
internet disconnected
local network
remote systems
APIs

Internet tools should be explicit.

Do not silently upload:

screenshots
passwords
private keys
entire filesystem
private emails

to random external services.

66. DIGITAL MEMORY OF ALMIR

Sali should gradually understand Almir's digital workflows.

Examples:

preferred project directories
development workflows
frequently used applications
frequently used tools
recurring tasks
common errors
VPS workflows
email workflows

This should emerge from actual interaction and observation rather than assumptions.

67. ALMIR'S ASSISTANT, NOT ALMIR'S REPLACEMENT

Sali should be highly autonomous at operating the computer, but it should remain aligned with Almir's objectives.

The architecture should distinguish:

Almir's explicit instruction
Sali's inference
Sali's recommendation
Sali's autonomous maintenance

Do not confuse these.

68. AUTONOMOUS MAINTENANCE

Sali may perform low-risk internal maintenance such as:

indexing
memory consolidation
cache cleanup
tool inventory
documentation indexing
health checks

More consequential changes should follow the configured authority model.

69. Sali's WORKSPACE

Primary autonomous workspace:

/home/almir/Desktop/sali-works

Sali may freely create, modify, organize, test, compile, and delete its own working artifacts there according to the execution policy.

Its core installation:

/home/almir/Desktop/sali

should be treated as protected runtime infrastructure.

Sali should be able to READ and understand its own source/configuration, but should not casually overwrite or delete its own core.

Controlled self-upgrades can be implemented later.

70. SELF-MODIFICATION

Do not allow:

model decides to edit itself
    ↓
immediately overwrites production Sali

Instead:

proposed change
    ↓
isolated workspace
    ↓
tests
    ↓
validation
    ↓
backup
    ↓
controlled deployment

Sali can eventually improve its own software, but through engineering discipline.

71. SELF-KNOWLEDGE

Sali should understand its architecture.

For example:

"My model is Qwen running through Ollama."

"My graph memory contains long-term relationships."

"My vector memory contains semantic retrieval."

"My event bus receives desktop events."

"My accessibility subsystem describes UI state."

"My vision subsystem interprets screenshots."

"My tool executor operates Linux."

This is self-modeling.

72. CONTEXT ASSEMBLY

Before answering a meaningful request:

current user request
    +
current task
    +
current desktop state
    +
current environment
    +
relevant graph nodes
    +
relevant experiences
    +
relevant procedures
    +
relevant tool knowledge
    +
relevant recent events
    +
relevant online research
    ↓
Context Assembly
    ↓
Qwen

Do not simply dump the entire memory database into the prompt.

73. REAL-TIME CONTEXT

If Almir is currently:

editing project X

and asks:

"Why isn't this working?"

Sali should already know:

active application
active project
recently changed files
recent terminal commands
recent errors
relevant tools
recent environment changes

This is the point of persistent perception.

74. EXAMPLE OF TRUE SALI BEHAVIOR

Almir opens VS Code.

Sali receives:

application.started

Accessibility:

VS Code
project = Project X

Filesystem:

Project X files changing.

Terminal:

npm run dev

Result:

build error.

Sali recognizes:

this is likely relevant.

It investigates.

Sali searches memory:

previous Project X errors

Finds:

similar issue solved previously.

Sali informs Almir:

"The current error looks similar to the dependency issue we fixed previously. I have not changed anything yet."

That is the desired behavior.

75. ANOTHER EXAMPLE

Almir types:

sudo systemctl restart nginx

The command fails.

Sali sees:

command
stderr
exit code

It knows:

nginx configuration may be invalid.

It can tell Almir:

"Nginx did not restart successfully. The error indicates a configuration problem. I haven't changed anything."

If Almir asks:

"Fix it."

Then Sali investigates and acts.

76. ANOTHER EXAMPLE — ONLINE LEARNING

Almir asks:

"Make this service work."

Sali investigates locally.

Local knowledge insufficient.

Sali:

searches official documentation
    ↓
learns configuration requirement
    ↓
checks local environment
    ↓
applies appropriate configuration
    ↓
tests
    ↓
succeeds
    ↓
stores procedure

Next time:

no web search required.
77. ANOTHER EXAMPLE — TOOL DISCOVERY

Sali discovers an unfamiliar executable.

Instead of ignoring it:

identify
    ↓
version
    ↓
help
    ↓
documentation
    ↓
test
    ↓
capability extraction
    ↓
graph memory

Later it becomes part of Sali's tool knowledge.

78. ANOTHER EXAMPLE — SECURITY

Sali observes:

newly exposed network service.

It checks:

process
port
service
configuration
known state

Then tells Almir:

"A new service is listening on port X. This differs from the machine's previous state."

It does not automatically attack or disable the service.

It investigates and informs first unless an explicit defensive policy authorizes automatic remediation.

79. LEARNING BUDGET

Continuous intelligence must be resource-aware.

Implement budgets for:

CPU
GPU
RAM
disk
network
LLM inference

For example:

lightweight monitoring:
    always active

semantic analysis:
    event driven

vision:
    on demand / event triggered

deep research:
    task driven

memory consolidation:
    scheduled/background

This is how Sali remains practical on a local machine.

80. MODEL STREAMING

Use Ollama's streaming capabilities.

The interface should support:

token streaming
cancellation
partial generation
tool-call events
tool results
reasoning state where appropriate
task progress

WebSocket events should allow the future GUI to display:

Sali is thinking/processing
Sali is using tool X
Sali received result
Sali learned something
Sali completed task

Do not expose private chain-of-thought.

Expose concise execution/progress events instead.

81. TERMINAL FIRST

The first user interface is the terminal.

Example:

$ sali

Then:

Almir:
    check why Docker isn't starting

Sali:

inspect environment
retrieve memories
inspect Docker
stream progress
execute tools
report result

The terminal is only the first interface.

The backend must be UI-independent.

82. FUTURE GUI

Later a desktop/web UI can connect to the same WebSocket backend.

Possible UI:

Chat
Live desktop state
Current task
Tool execution
Memory
Graph
Learning
Environment
Processes
Services
Network
Email
VPS
Logs

Do NOT couple core intelligence to the terminal UI.

83. MULTIPLE CLIENTS

Eventually Sali may have:

terminal
desktop application
web interface
mobile interface
browser interface

All should communicate with the same Sali Core.

Do not create a separate Sali for each interface.

84. PERSISTENCE

When the computer shuts down:

Sali should preserve:

memories
graph
learned tools
procedures
tasks
experiences
important events
environment snapshots

When it starts again:

restore state
    ↓
detect what changed while offline
    ↓
synchronize environment
    ↓
continue
85. Sali SHOULD FEEL CONTINUOUS

The conceptual lifecycle is:

BOOT
  ↓
AWAKE
  ↓
OBSERVE
  ↓
LEARN
  ↓
WORK
  ↓
REMEMBER
  ↓
REST
  ↓
BOOT AGAIN
  ↓
CONTINUE

Not:

question
  ↓
answer
  ↓
disappear
86. IMPORTANT DISTINCTION

Sali does not need to literally be conscious to be extremely sophisticated.

Implement the engineering characteristics that produce the behavior Almir wants:

persistent state
persistent memory
self-model
environment model
continuous perception
event processing
attention
learning
reflection
planning
tool use
experience accumulation
temporal awareness

Do not fake consciousness with a system prompt.

Build the mechanisms.

87. ENGINEERING QUALITY

This should be production-quality architecture.

Use:

strong typing
structured schemas
async architecture
queues
event bus
connection pooling
retries
timeouts
observability
structured logging
health checks
migrations
tests
failure recovery
graceful shutdown
systemd integration

Avoid:

giant Python file
global mutable state
hard-coded tool lists
hard-coded memory
arbitrary shell execution
prompt-only security
infinite loops
uncontrolled background inference
88. CORE SERVICES

Design the system as clear components.

Potential services:

sali-core
sali-event-bus
sali-perception
sali-accessibility
sali-vision
sali-memory
sali-graph
sali-retrieval
sali-context
sali-tools
sali-executor
sali-learning
sali-email
sali-web
sali-health

Do not necessarily make each a separate process.

Choose process boundaries based on reliability and complexity.

89. EVENT-DRIVEN ARCHITECTURE

Prefer:

event
  ↓
handler
  ↓
state update
  ↓
memory
  ↓
attention
  ↓
reasoning if needed

rather than:

infinite loop
  ↓
ask LLM
  ↓
infinite loop
90. FINAL VISION

The ultimate behavior should be:

Sali starts when Kali starts.

Sali remains present.

Sali knows who it is.

Sali knows who Almir is.

Sali knows which computer it lives in.

Sali knows its current environment.

Sali sees useful desktop events.

Sali can inspect the screen when appropriate.

Sali can understand accessibility information.

Sali can use vision.

Sali can use terminal tools.

Sali can use Linux tools.

Sali can install tools.

Sali can learn tools.

Sali can research online.

Sali can remember what it learned.

Sali can use what it learned later.

Sali can remember failures.

Sali can remember successful solutions.

Sali can understand the computer's history.

Sali can understand current state.

Sali can notice meaningful changes.

Sali can tell Almir useful things proactively.

Sali can work on tasks.

Sali can manage authorized remote systems.

Sali can work with email.

Sali can continuously improve its knowledge.

Sali can remain useful when the internet is unavailable.

Sali's brain remains local.

Sali's memory remains local.

Sali's private information remains local.

91. THE CENTRAL PHILOSOPHY

Do not build an AI that merely answers Almir.

Build an intelligence that understands the environment in which Almir works.

Do not make Sali remember everything blindly.

Make Sali understand what is important.

Do not make Sali watch every pixel continuously.

Give Sali efficient perception.

Do not make Sali run the LLM constantly.

Give Sali an attention system.

Do not make Sali depend on the internet.

Let the internet teach Sali.

Do not make Sali forget what it learned.

Turn experience into durable knowledge.

Do not make Sali blindly modify itself.

Give it controlled engineering mechanisms for improvement.

Do not make the model responsible for security boundaries.

Put security boundaries in infrastructure.

92. IMPLEMENTATION ORDER

Before changing code:

Inspect the current Sali repository completely.
Understand the existing memory system.
Understand the graph schema.
Understand the current Ollama integration.
Understand current tool calling.
Understand the existing terminal interface.
Identify what already exists.
Do not duplicate existing components.

Then implement in phases.

PHASE 1:

Persistent Sali Core
Ollama warm connection
WebSocket infrastructure
event bus
runtime state

PHASE 2:

environment discovery
process monitoring
service monitoring
filesystem events

PHASE 3:

Linux desktop accessibility
AT-SPI / applicable accessibility infrastructure
application/window state

PHASE 4:

vision subsystem
screenshot capture
region capture
active-window capture
event-driven vision

PHASE 5:

attention system
event importance
proactive notification

PHASE 6:

deep graph memory integration
temporal memory
episodic memory
procedural memory
environment memory
self memory

PHASE 7:

Tool Intelligence
discovery
installation
learning
procedures
tool relationships

PHASE 8:

online research
documentation learning
knowledge provenance
local knowledge consolidation

PHASE 9:

email integration
VPS integration
remote environment modeling

PHASE 10:

learning queue
self-reflection
environment baselines
anomaly detection
continuous improvement

PHASE 11:

future GUI
live desktop state
memory explorer
graph explorer
task monitor
93. FIRST TASK

Do NOT immediately start writing hundreds of files.

First:

inspect the existing Sali implementation.

Then produce an architecture assessment:

what already exists
what is missing
what should be changed
what should be preserved
proposed architecture
database changes
graph changes
event schema
service architecture
security model
startup model
implementation phases

Then begin implementation incrementally.

Every subsystem must have tests.

94. SUCCESS TEST

Eventually I should be able to boot Kali.

Without manually launching Sali's model every time:

Sali starts.

Then open applications.

Sali receives environmental events.

Open terminal.

Sali knows the terminal is active.

Open browser.

Sali knows the browser became active.

Modify a project.

Sali sees the filesystem changes.

Run a command.

Sali can understand the result.

Ask Sali a question.

The model is already warm.

The response streams through WebSocket.

Sali can call tools.

Sali can inspect its memory.

Sali can inspect its environment.

Sali can search online when necessary.

Sali can learn.

Sali remembers what it learned.

Later, ask about the same problem.

Sali retrieves the previous experience instead of starting from zero.

That is the target.

FINAL DEFINITION

Sali is:

A persistent local AI intelligence living inside Almir's Kali Linux environment.

Its:

Qwen 35B model = brain

Graph memory = long-term relational memory

Vector memory = semantic memory

Episodic memory = experiences

Procedural memory = learned skills

Environment model = understanding of its digital home

Accessibility = structured digital perception

Vision = visual perception

Event bus = nervous system

WebSocket = real-time communication

Tool registry = knowledge of capabilities

Execution broker = hands

Internet tools = external information source

Learning system = continuous education

Attention system = decides what deserves thought

Context engine = decides what the brain should remember right now

Terminal = first interface

Future GUI = another interface

The objective is not to make Sali pretend to be alive.

The objective is to build the architecture that allows Sali to behave like a persistent, continuously learning intelligence with a genuine model of its environment.

Build the mechanisms.

Do not fake the result with prompts.


### One thing I would add to the architecture

The **most important new component** you're asking for is an **Attention + World-State Engine**.

Without it, "always running" just means an expensive model sitting in VRAM.

With it, Sali becomes much more interesting:

```text
             KALI LINUX
                 │
        ┌────────┴─────────┐
        │                  │
   Desktop Events      System Events
        │                  │
        └────────┬─────────┘
                 ▼
        ┌──────────────────┐
        │  WORLD STATE     │
        │                  │
        │ "What is         │
        │ happening now?"  │
        └────────┬─────────┘
                 ▼
        ┌──────────────────┐
        │    ATTENTION     │
        │                  │
        │ "Does this       │
        │ matter?"         │
        └────────┬─────────┘
                 │
        ┌────────┴─────────┐
        ▼                  ▼
      Ignore            Investigate
                           │
                           ▼
                    ┌──────────────┐
                    │ QWEN 35B     │
                    │ already warm │
                    └──────┬───────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
           Memory        Tools        Vision
              │            │            │
              └────────────┼────────────┘
                           ▼
                        Learn
                           │
                           ▼
                     Update World

That's the distinction between "Ollama running in the background" and the kind of persistent Sali you're envisioning.

Also, I would not make Sali's model process continuously generate thoughts 24/7. That's wasteful and will eventually make the system noisy. Let the cheap event infrastructure run continuously, keep Qwen warm, and wake its expensive reasoning process when the attention system decides something is worth thinking about. That gives you the "Sali is always here" property without burning your 4070 doing useless inference all day.