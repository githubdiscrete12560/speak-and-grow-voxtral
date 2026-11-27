import { cpSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(fileURLToPath(import.meta.url));
const publish = join(root, "dist");

mkdirSync(publish, { recursive: true });

for (const file of ["index.html", "books.css", "books.js", "config.js", "_redirects", "assets/67311039_30-10-2024_07-51-36.jpg", "assets/hero-meditation.webp", "assets/hero-meditation-mobile.webp"]) {
  const destination = join(publish, file);
  mkdirSync(dirname(destination), { recursive: true });
  cpSync(join(root, file), destination);
}
