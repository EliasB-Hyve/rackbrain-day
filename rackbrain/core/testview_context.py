from Testviewlog import get_latest_failed_run


def add_testview_context(error_event) -> None:
    """
    Enrich ErrorEvent with latest SLT/TestView info from hyvetest.
    """
    sn = error_event.sn
    if not sn:
        return

    try:
        latest = get_latest_failed_run(sn)
    except Exception as exc:
        print(f"[WARN] Failed to fetch TestView SLT context for {sn}: {exc}")
        return

    if not latest:
        return

    slt_id = latest.get("slt_id")
    failed_testset = latest.get("failed_testset")
    failed_testcase = latest.get("failed_testcase")
    failure_message = latest.get("failure_message")
    same_fail = latest.get("same_failure_count")
    testcases_list = latest.get("testcases") or []

    if error_event.server_status_id is None and slt_id is not None:
        error_event.server_status_id = slt_id

    if not error_event.failed_testset and failed_testset:
        error_event.failed_testset = failed_testset

    if not error_event.failure_message and failure_message:
        error_event.failure_message = failure_message

    error_event.db_latest_slt_id = slt_id
    error_event.db_latest_failed_testset = failed_testset
    error_event.db_failed_testcase = failed_testcase
    error_event.db_failed_testcase_list = testcases_list
    error_event.db_same_failure_count = same_fail

    print("[DEBUG] TestView context for SN %s:" % sn)
    print("        slt_id              =", slt_id)
    print("        failed_testset      =", failed_testset)
    print("        failed_testcase     =", failed_testcase)
    print("        same_failure_count  =", same_fail)
    print("        testcases_list      =", testcases_list)
