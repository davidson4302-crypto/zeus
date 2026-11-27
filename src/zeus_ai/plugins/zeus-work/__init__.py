"""Zeus native durable-work plugin entry point."""


def register(ctx):
    from .runtime import Runtime, GUIDANCE
    runtime=Runtime(ctx)
    passive=ctx.get_config('passive',False) is True or ctx.get_config('execution_enabled',True) is False
    check={"type":"object","properties":{
        "kind":{"type":"string","enum":["contains","not_contains","json_equals","json_count","sha256","min_bytes"]},
        "value":{},"path":{"type":"array","items":{}}},"required":["kind","value"],"additionalProperties":False}
    criterion={"type":"object","properties":{
        "id":{"type":"string"},"description":{"type":"string"},
        "artifact":{"type":"string","description":"Relative name in this assignment's managed artifact directory. No format is assumed."},
        "destination":{"type":"string","description":"For an output requested at an explicit absolute local path: that exact path from the authentic original/correction request. Native writes require this binding. The managed artifact must contain the SAME final bytes, not a narrative receipt. Checkpoint/verify/complete independently re-read and compare the actual regular destination file. Omit for managed-only outputs. Derived preparation cannot add outside destinations."},
        "checks":{"type":"array","items":check}},"required":["id","description"],"additionalProperties":False}
    step={"type":"object","properties":{
        "id":{"type":"string"},"description":{"type":"string"},
        "depends_on":{"type":"array","items":{"type":"string"}},
        "criteria":{"type":"array","minItems":1,"items":{"type":"string"},"description":"Required criterion IDs independently checked before this step can become checked. For reading/research, retain a verifiable evidence artifact or combine with an output step."}},"required":["id","description","criteria"],"additionalProperties":False}
    properties={
        "action":{"type":"string","enum":["create","create_derived","status","history","revise","stop","resume","artifact","read_artifact","read_parent_artifact","checkpoint","report","ask_optional","verify","complete","schedule","doctor","effect_prepare","effect_execute","effect_reconcile","observe_setting"]},
        "id":{"type":"string","description":"Existing canonical work ID. Reuse through corrections/restarts."},
        "title":{"type":"string"},"criteria":{"type":"array","items":criterion},"steps":{"type":"array","items":step},
        "continuation":{"type":"string","enum":["automatic","never"],"description":"create: default automatic continuation of accepted unfinished work. never requires the owner's explicit no-background instruction."},
        "flight_watch_id":{"type":"string","description":"schedule: when only monitoring remains, use the trip ID returned by life flight add. The existing trip watcher owns its layover, landing and pickup texts."},
        "flight_watch_steps":{"type":"array","items":{"type":"string"},"description":"Only the unchecked steps owned by this trip watcher. Other steps can continue. Use [] if every outstanding step is independent work."},
        "external_ref":{"description":"For a task wholly owned by an existing life flight watch, set system to life-flight and id to the exact returned trip ID.","type":"object","properties":{"system":{"type":"string"},"id":{"type":"string"},"url":{"type":"string"}},"required":["system","id"],"additionalProperties":False},
        "origin_source_ref":{"type":"string","description":"Optional existing source pointer. Not authorization."},
        "parent_id":{"type":"string","description":"create_derived only: existing owner-authorized parent work ID."},
        "intent_digest":{"type":"string","description":"create_derived only: lowercase SHA-256 of the observed preparation intent. Internal trigger, not a human request."},
        "kind":{"type":"string","enum":["quiet_review","local_preparation"],"description":"create_derived requires root's sourced preparation policy. Local artifacts only; no new outside action or notification."},
        "name":{"type":"string","description":"Relative artifact name for artifact/read_artifact/effect_prepare."},
        "content":{"type":"string","description":"UTF-8 artifact content. Writes are atomic and invalidate prior proof."},
        "step_id":{"type":"string"},"note":{"type":"string"},"next_action":{"type":"string"},
        "disposition":{"type":"string","enum":["unfinished","needs_person","uncertain"]},
        "owner_message":{"type":"string","description":"report only: an explicit short plain-language question about a real owner decision or missing access. Required for needs_person. Never include private next_action, job IDs, builder instructions, no-resend notes or internal checks. If more agent/builder work is needed, use unfinished instead."},
        "missing":{"type":"string","description":"Timed checkpoint only: one short line about what you could not verify or finish."},
        "answer_options":{"type":"array","items":{"type":"string"},"description":"ask_optional on timed work only: two to four short distinct preference labels. Use owner_message for the question. Keep working without waiting."},
        "tool":{"type":"string","description":"For effect_prepare only: installed native effect handler."},
        "setting_inputs":{"type":"array","description":"For a Skool settings job, freeze each distinct community/category and its original Gmail source at creation. After the provider displays each confirmation or login return, call observe_setting while that page is open. The work tool reads it independently.","items":{"type":"object","properties":{"mailbox":{"type":"string"},"message_id":{"type":"string"},"community":{"type":"string"},"category":{"type":"string","enum":["broadcast","digest","daily","event"]},"link_hash":{"type":"string"}},"required":["mailbox","message_id","community","category"],"additionalProperties":False}},
        "tool_args":{"type":"object","description":"Frozen exact native effect arguments. Source authority is captured by the runtime, not supplied here."},
        "effect_id":{"type":"string"}}
    schema={"name":"zeus_work","description":"Own a substantive multistep request durably. create freezes the authentic native user source, owner, original criteria, dependency steps and optional existing task reference; it deduplicates the source. status exposes current work and fresh artifact verification. artifact writes inside this job; checkpoint requires independent passing criteria. revise updates the SAME job and fences old workers. report saves incomplete progress, never claims completion. complete re-reads exact final bytes and refuses missing criteria, unchecked steps or uncertain effects. schedule saves a durable native cron continuation; local CLI return and native transport return are distinct. stop persists before scheduler pause; resume needs an explicit current user request. doctor detects missing wake/stranded workers. Effect actions are optional exact installed native-handler integrations; a timeout or absent read never authorizes resend. No credentials, approval flags or invented sources accepted.","parameters":{"type":"object","properties":properties,"required":["action"],"additionalProperties":False}}
    if not passive:
        ctx.register_tool(name="zeus_work",toolset="todo",schema=schema,handler=runtime.tool)
        ctx.register_hook("pre_llm_call",runtime.sources.bind)
        ctx.register_hook("pre_llm_call",runtime.before_turn)
        ctx.register_hook("post_llm_call",runtime.after_turn)
        ctx.register_hook("on_session_end",runtime.session_end)
        ctx.register_hook("on_session_end",runtime.sources.clear)
        ctx.register_hook("post_gateway_processing",runtime.gateway_processed)
        ctx.register_hook("post_gateway_admission",runtime.admission)
        ctx.register_hook("post_gateway_intake",runtime.deadline_intake)
        ctx.register_hook("transform_llm_output",runtime.transform)
        ctx.register_middleware("tool_execution",runtime.guard)
        ctx.register_middleware("llm_request",runtime.deadline_clock)
        ctx.register_system_prompt_section("zeus-work.usage",GUIDANCE,position="after_memory",max_chars=650)
    def setup(parser):
        parser.add_argument("--id",default=None)
        parser.add_argument("--doctor",action="store_true")
        parser.add_argument("--deadline-pass",metavar="WORK_ID",help="Advance one already-owned timed return without a model")
        parser.add_argument("--maintenance",action="store_true",help="Bounded existing-intent repair, at most 3 records; no model, sender or external task scan")
    def inspect(args):
        # Local native CLI is the owner's inspection surface; no remote command or
        # mutating repair is registered here.
        from .runtime import home
        from .store import Store,canonical
        from .native import Native
        store=Store(home())
        if args.deadline_pass:
            print(canonical(Native(home()).deadline_pass(args.deadline_pass)))
            return 0
        value=Native(home()).maintenance(limit=3) if args.maintenance else Native(home()).doctor() if args.doctor else store.get(args.id) if args.id else store.all()
        print(canonical(value))
        return 0
    ctx.register_cli_command("zeus-work-inspect","Read Zeus execution records and narrow health checks",setup,inspect)
    return runtime
