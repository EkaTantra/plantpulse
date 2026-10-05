"""
Assemble the public demo site (Streamlit running in the browser via stlite) for GitHub Pages.

  python scripts/build_pages.py <out_dir>

The site serves site/index.html plus the demo-mode app (app/streamlit_app.py, app/local_engine.py)
and the synthetic CSVs. Publish <out_dir> as the gh-pages branch.
"""
import glob
import os
import shutil
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def build(out):
    os.makedirs(os.path.join(out, "app"), exist_ok=True)
    os.makedirs(os.path.join(out, "data"), exist_ok=True)
    shutil.copy(os.path.join(ROOT, "site", "index.html"), out)
    for f in ("streamlit_app.py", "local_engine.py", "architecture.png"):
        shutil.copy(os.path.join(ROOT, "app", f), os.path.join(out, "app"))
    for f in glob.glob(os.path.join(ROOT, "data", "*.csv")):
        shutil.copy(f, os.path.join(out, "data"))
    open(os.path.join(out, ".nojekyll"), "w").close()
    print("site built in", out)


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "_site"))
