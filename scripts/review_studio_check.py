"""Validate and derive observations using the adjacent NexGen checkout, read-only."""
from argparse import ArgumentParser
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from studio.demo import populate
from studio.integrations import legacy_export
from studio.store import Store, doc


def main():
    parser = ArgumentParser()
    parser.add_argument("checkout", type=Path)
    args = parser.parse_args()
    module = (args.checkout / "platform/core/analyzer.js").resolve()
    if not module.is_file():
        parser.error("Pass a NexGen unified-platform checkout with platform/core/analyzer.js.")
    source = """
      import fs from 'node:fs';
      const { validateSessionRecord, deriveMetrics } = await import(process.argv[1]);
      const record = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
      const validation = validateSessionRecord(record);
      if (!validation.ok) throw Error(JSON.stringify(validation));
      const results = deriveMetrics(record);
      console.log(JSON.stringify({ valid: validation.ok, warnings: validation.warnings,
                                   words: results.words.value.count, sample: record.sample }));
    """
    with TemporaryDirectory() as folder:
        store = Store(Path(folder) / "example.sqlite")
        lesson = populate(store)
        store.approve(lesson, "Passed", "Fictional teacher", "")
        output = Path(folder) / "review.json"
        output.write_text(doc(legacy_export(store.load(lesson))), encoding="utf-8")
        subprocess.run(["node", "--input-type=module", "-e", source, module.as_uri(), str(output)],
                       check=True)


if __name__ == "__main__":
    main()
