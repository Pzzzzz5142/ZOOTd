. as $decision
| .schema_version == 1
and .decision == "FIGHT"
and .evidence.client_proxy_policy.ground_truth == "game-client"
and .evidence.client_proxy_policy.unknown_allowed == true
and .evidence.client_proxy_policy.positive_ledger_required == false
and .evidence.client_proxy_policy.local_quarantine_overrides == true
and .evidence.client_proxy_policy.preflight == "one-fight-plus-use-prts-success-check"
and .evidence.client_proxy_policy.preflight_consumes_sanity == true
and .evidence.activity_window_policy.source == "maa-stage-activity-v2"
and .evidence.activity_window_policy.half_open_interval == true
and (.evidence.execution_candidates | type == "array" and length > 0)
and all(.evidence.execution_candidates[];
    (.stage_code | type == "string"
        and test("^[A-Za-z0-9][A-Za-z0-9@._-]{0,63}$"))
    and (.item_id | type == "string" and test("^[0-9]{1,20}$"))
    and (.activity_instance | type == "string" and test("^[0-9a-f]{24}$"))
    and .series == 0
    and .medicine == 0
    and .medicine_expire_days == 2
    and .stone == 0
    and .drop_goal == null
    and .authorization == "game-client-proxy-required")
and all(.evidence.execution_candidates[];
    . as $execution
    | any($decision.candidates[]?;
        .stage_code == $execution.stage_code
        and .item_id == $execution.item_id
        and .activity_instance == $execution.activity_instance
        and .eligible == true
        and .rejected_by == []))
and .selected_stage == .evidence.execution_candidates[0].stage_code
and .selected_item == .evidence.execution_candidates[0].item_id
and .activity.instance_id == .evidence.execution_candidates[0].activity_instance
and .drop_goal == .evidence.execution_candidates[0].drop_goal
and .series == .evidence.execution_candidates[0].series
and .medicine == .evidence.execution_candidates[0].medicine
and .medicine_expire_days == .evidence.execution_candidates[0].medicine_expire_days
and .stone == .evidence.execution_candidates[0].stone
