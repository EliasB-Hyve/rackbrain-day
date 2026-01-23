# rackbrain/adapters/hyvetest_client.py

import logging
from typing import Any, Dict, Optional

import pymysql
from database_config import host as DB_HOST, user as DB_USER, passwd as DB_PASS, db as DB_NAME


def fetch_server_details_from_db(sn: str) -> Optional[Dict[str, Any]]:
    """
    Look up SLT context for a server SN in hyvetest (EVE DB).

    Returns a dict with keys:
      sn, server_status_id, server_ok, pos, rack_sn, model, customer_ipn,
      test_rack_sn, tm2_ver, tester_email, started, finished,
      server_error_detail, failed_testcase, failed_testset,
      failure_message, guti
    or None if not found / error.
    """
    conn = None
    cursor = None
    try:
        if not (DB_HOST and DB_USER and DB_PASS and DB_NAME):
            missing = []
            if not DB_HOST:
                missing.append("RACKBRAIN_DB_HOST")
            if not DB_USER:
                missing.append("RACKBRAIN_DB_USER")
            if not DB_PASS:
                missing.append("RACKBRAIN_DB_PASS")
            if not DB_NAME:
                missing.append("RACKBRAIN_DB_NAME")
            print(
                "[INFO] DB lookup skipped: missing env var(s): %s"
                % (", ".join(missing) if missing else "unknown")
            )
            return None

        conn = pymysql.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASS,
            database=DB_NAME,
            charset="utf8mb4",
        )
        cursor = conn.cursor()

        query = """
        SELECT 
            Server.sn_tag AS sn, 
            ServerStatus.id AS ssid, 
            ServerStatus.ok AS ss_ok, 
            Server.position AS pos, 
            Rack.sn_tag AS rack_sn, 
            ServerStatus.states -> '$.sfcs.model' AS model, 
            ServerStatus.states -> '$.sfcs.customerIpn' AS customer_ipn, 
            ServerStatus.states -> '$.meta."rack_sn"' AS test_rack_sn, 
            ServerStatus.states -> '$.meta."code_version"' AS TM2_ver, 
            ServerStatus.states -> '$.operation_records[0]."user_email"' AS tester_email, 
            ServerStatus.started, 
            ServerStatus.finished, 
            servererror.detail, 
            JSON_UNQUOTE(ServerStatus.states -> '$.jar_deliver."testErrorCode"') as Failed_Testcase, 
            JSON_UNQUOTE(ServerStatus.states -> '$.jar_deliver."associatedTestSetName"') as Failed_Testset, 
            JSON_UNQUOTE(ServerStatus.states -> '$.jar_deliver."failureMessage"') as failure_Message, 
            ServerStatus.states -> '$.jar_deliver.associatedTestSetGuti' AS guti
        FROM Server 
        JOIN ServerStatus ON Server.serverstatus_id = ServerStatus.id 
        LEFT JOIN servererror ON ServerStatus.id = servererror.serverstatus_id 
        LEFT JOIN Rack ON Server.rack_id = Rack.id 
        WHERE Server.sn_tag = %s
        """

        cursor.execute(query, (sn,))
        row = cursor.fetchone()
        if not row:
            return None

        columns = [
            "sn",
            "server_status_id",
            "server_ok",
            "pos",
            "rack_sn",
            "model",
            "customer_ipn",
            "test_rack_sn",
            "tm2_ver",
            "tester_email",
            "started",
            "finished",
            "server_error_detail",
            "failed_testcase",
            "failed_testset",
            "failure_message",
            "guti",
        ]

        return dict(zip(columns, row))

    except pymysql.MySQLError as e:
        logging.exception("Database error occurred: %s", e)
        print(f"Database error: {e}")
        return None
    except Exception as e:
        logging.exception("Unexpected error occurred: %s", e)
        print(f"Error fetching server details: {e}")
        return None
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()
