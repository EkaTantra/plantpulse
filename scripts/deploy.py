"""
PlantPulse one-shot deployer.

Uses the same ~/.snowflake/connections.toml that CoCo CLI and Snowflake CLI use.

  pip install "snowflake-connector-python[pandas]" numpy pandas
  python scripts/deploy.py --connection <your_connection_name>            # everything
  python scripts/deploy.py --connection <name> --only 03_ml_model.sql     # one step

Steps: generate data -> 00 setup -> PUT CSVs -> 01 load -> 02..08 SQL (03b after 03) -> 10 ASK_OEE ->
       11 Cortex Agent -> upload app -> 09 Streamlit
  --app-only re-uploads the Streamlit app and re-runs 09 only.
"""
import argparse
import glob
import os
import subprocess
import sys
import time

import snowflake.connector

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SQL_DIR = os.path.join(ROOT, "sql")
DATA_DIR = os.path.join(ROOT, "data")
APP_DIR = os.path.join(ROOT, "app")


def run_sql_file(conn, path):
    print(f"\n=== {os.path.basename(path)} ===")
    t0 = time.time()
    with open(path, encoding="utf-8") as f:
        sql = f.read()
    for cur in conn.execute_string(sql, remove_comments=False):
        try:
            rows = cur.fetchmany(12)
        except Exception:
            rows = []
        q = (cur.query or "").strip().splitlines()[0][:90] if cur.query else ""
        print(f"  ok  {q}")
        for r in rows[:12]:
            print("      ", r)
    print(f"  ({time.time() - t0:.1f}s)")


def put(conn, local_glob, stage, compress=True):
    cur = conn.cursor()
    for p in sorted(glob.glob(local_glob)):
        uri = p.replace("\\", "/")
        cur.execute(f"PUT 'file://{uri}' {stage} AUTO_COMPRESS={'TRUE' if compress else 'FALSE'} OVERWRITE=TRUE")
        print(f"  PUT {os.path.basename(p)} -> {stage}")


def deploy_app(conn):
    print("\n=== uploading Streamlit app ===")
    conn.cursor().execute("CREATE STAGE IF NOT EXISTS PLANTPULSE.ANALYTICS.APP_STAGE DIRECTORY = (ENABLE = TRUE)")
    for f in ["streamlit_app.py", "local_engine.py", "environment.yml", "architecture.png"]:
        put(conn, os.path.join(APP_DIR, f), "@PLANTPULSE.ANALYTICS.APP_STAGE", compress=False)
    put(conn, os.path.join(APP_DIR, ".streamlit", "config.toml"), "@PLANTPULSE.ANALYTICS.APP_STAGE/.streamlit", compress=False)
    run_sql_file(conn, os.path.join(SQL_DIR, "09_streamlit_app.sql"))
    print("\nDone. Open Snowsight > Projects > Streamlit > PLANTPULSE_COMMAND_CENTER")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--connection", "-c", default=os.environ.get("SNOWFLAKE_DEFAULT_CONNECTION_NAME", "default"))
    ap.add_argument("--only", help="run a single SQL file, e.g. 03_ml_model.sql")
    ap.add_argument("--skip-ml", action="store_true",
                    help="skip ML training in 03_ml_model.sql (rules-only scoring) and 03b_ml_validation.sql")
    ap.add_argument("--app-only", action="store_true", help="only upload the Streamlit app and run 09")
    args = ap.parse_args()

    if not os.path.exists(os.path.join(DATA_DIR, "sensor_readings.csv")):
        subprocess.check_call([sys.executable, os.path.join(ROOT, "data_gen", "generate_data.py"), "--out", DATA_DIR])

    conn = snowflake.connector.connect(connection_name=args.connection)
    print(f"Connected as {conn.user} to {conn.account}")

    if args.only:
        run_sql_file(conn, os.path.join(SQL_DIR, args.only))
        return
    if args.app_only:
        deploy_app(conn)
        return

    run_sql_file(conn, os.path.join(SQL_DIR, "00_setup.sql"))
    print("\n=== uploading data ===")
    put(conn, os.path.join(DATA_DIR, "*.csv"), "@PLANTPULSE.ERP.LANDING")
    for f in ["01_load.sql", "02_features.sql", "03_ml_model.sql", "03b_ml_validation.sql", "04_oee.sql",
              "05_cortex_search.sql", "06_semantic_view.sql", "07_automation.sql", "08_streaming_and_tasks.sql",
              "10_ask_oee.sql", "11_cortex_agent.sql"]:
        if f == "03b_ml_validation.sql" and args.skip_ml:
            print("=== 03b_ml_validation.sql skipped (--skip-ml) ===")
            continue
        if f == "03_ml_model.sql" and args.skip_ml:
            # still need the scoring procedure without the model
            conn.cursor().execute("USE DATABASE PLANTPULSE")
            sql = open(os.path.join(SQL_DIR, f), encoding="utf-8").read()
            sql = sql.split("CREATE OR REPLACE SNOWFLAKE.ML.CLASSIFICATION")[0] + \
                  "CREATE TABLE IF NOT EXISTS" + sql.split("CREATE TABLE IF NOT EXISTS", 1)[1]
            sql = sql.replace("CALL ANALYTICS.SCORE_ASSETS(TRUE);", "CALL ANALYTICS.SCORE_ASSETS(FALSE);")
            for _ in conn.execute_string(sql):
                pass
            print("=== 03_ml_model.sql (rules-only) ===")
            continue
        run_sql_file(conn, os.path.join(SQL_DIR, f))

    deploy_app(conn)


if __name__ == "__main__":
    main()
