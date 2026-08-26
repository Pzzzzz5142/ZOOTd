def epoch: sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601;
def run_reason:
    . == "WEEK_DEADLINE_CATCH_UP"
    or . == "FULL_WEEK_ACTIVITY_MONDAY_CATCH_UP"
    or . == "SOURCE_UNCERTAIN_MONDAY_CATCH_UP"
    or . == "CURRENT_WINDOW_HAS_NO_ACTIVITY"
    or . == "NO_SAFE_FREE_SLOT_MONDAY_CATCH_UP";
def wait_reason:
    . == "FULL_WEEK_ACTIVITY_MONDAY"
    or . == "SOURCE_UNCERTAIN_WAIT_FOR_MONDAY"
    or . == "FUTURE_NO_ACTIVITY_WINDOW"
    or . == "NO_SAFE_FREE_SLOT_MONDAY";

. as $decision
| .schema_version == 1
and .kind == "annihilation-plan"
and (.decision == "RUN" or .decision == "WAIT" or .decision == "COMPLETE" or .decision == "BLOCKED" or .decision == "MISSED")
and (.generated_at | type == "string" and epoch > 0)
and .client == "Official"
and (.account | type == "string" and test("^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"))
and (.week_start_game_day | type == "string" and test("^[0-9]{4}-[0-9]{2}-[0-9]{2}$"))
and (.week_start | type == "string" and epoch > 0)
and (.week_end | type == "string" and epoch > ($decision.week_start | epoch))
and .stage == "Annihilation"
and .medicine == 0
and .medicine_expire_days == 2
and .stone == 0
and .times_per_transaction == 1
and .series == 1
and (.max_transactions_per_run | type == "number" and . == floor and . >= 1 and . <= 20)
and (.execution_budget_seconds | type == "number" and . == floor and . >= 60 and . <= 21600)
and .transaction_timeout_seconds == 1800
and .authorization == "game-client-weekly-annihilation"
and (if .decision == "COMPLETE" or .decision == "BLOCKED" or .decision == "MISSED" then
        .reason == (if .decision == "COMPLETE" then
                        "CLIENT_WEEKLY_CAP_RECORDED"
                    elif .decision == "MISSED" then
                        "INSUFFICIENT_WEEK_WINDOW"
                    else
                        "CLIENT_ANNIHILATION_PROXY_UNSTABLE"
                    end)
        and .due_at == null
        and .execute_before == null
     elif .decision == "RUN" then
        (.reason | run_reason)
        and (.due_at | type == "string"
            and epoch >= ($decision.week_start | epoch)
            and epoch <= ($decision.generated_at | epoch))
        and (.execute_before | type == "string"
            and epoch > ($decision.generated_at | epoch)
            and epoch >= (($decision.generated_at | epoch) + $decision.transaction_timeout_seconds)
            and epoch <= ($decision.week_end | epoch)
            and epoch <= (($decision.generated_at | epoch) + $decision.execution_budget_seconds))
     else
        (.reason | wait_reason)
        and (.due_at | type == "string"
            and epoch > ($decision.generated_at | epoch)
            and epoch >= ($decision.week_start | epoch)
            and epoch < ($decision.week_end | epoch))
        and (.execute_before | type == "string"
            and epoch > ($decision.due_at | epoch)
            and epoch <= ($decision.week_end | epoch)
            and epoch <= (($decision.due_at | epoch) + $decision.execution_budget_seconds))
     end)
and (.evidence | type == "object"
    and .execution_budget_seconds == $decision.execution_budget_seconds)
