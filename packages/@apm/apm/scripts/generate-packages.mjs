import * as fs from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const CLI_ROOT = resolve(fileURLToPath(import.meta.url), "../..");
const PACKAGES_ROOT = resolve(CLI_ROOT, "..");
const REPO_ROOT = resolve(PACKAGES_ROOT, "../..");
const MANIFEST_PATH = resolve(CLI_ROOT, "package.json");

const rootManifest = JSON.parse(fs.readFileSync(MANIFEST_PATH, "utf-8"));

/**
 * Maps npm platform/arch names to APM release archive names.
 * npm uses win32/x64; releases use windows/x86_64.
 */
function getArchiveName(npmPlatform, npmArch) {
	const platformMap = { win32: "windows", darwin: "darwin", linux: "linux" };
	const archMap = { x64: "x86_64", arm64: "arm64" };
	return `apm-${platformMap[npmPlatform]}-${archMap[npmArch]}`;
}

function copyBinaryToNativePackage(npmPlatform, npmArch) {
	const buildName = `cli-${npmPlatform}-${npmArch}`;
	const packageRoot = resolve(PACKAGES_ROOT, buildName);
	const packageName = `@apm/${buildName}`;
	const isWindows = npmPlatform === "win32";

	// Update the package.json manifest
	const { version, license, repository, engines, homepage, publishConfig, description } = rootManifest;
	const binaryFile = isWindows ? "apm.exe" : "apm";
	const manifest = JSON.stringify(
		{
			name: packageName,
			description,
			version,
			license,
			repository,
			engines,
			homepage,
			os: [npmPlatform],
			cpu: [npmArch],
			publishConfig,
			files: [binaryFile, "_internal", "README.md", "LICENSE"],
		},
		null,
		2,
	);

	const manifestPath = resolve(packageRoot, "package.json");
	console.info(`Update manifest ${manifestPath}`);
	fs.writeFileSync(manifestPath, manifest);

	// Locate the binary by probing candidate directories in priority order:
	//   1. dist/{name}/apm          -- local build (build-binary.sh)
	//   2. artifacts/{name}/dist/{name}/apm  -- CI downloaded artifact
	const archiveName = getArchiveName(npmPlatform, npmArch);
	const ext = isWindows ? ".exe" : "";
	const candidates = [
		resolve(REPO_ROOT, "dist", archiveName, `apm${ext}`),
		resolve(REPO_ROOT, "artifacts", archiveName, "dist", archiveName, `apm${ext}`),
	];
	const binarySource = candidates.find((p) => fs.existsSync(p));
	if (!binarySource) {
		console.error(`Binary not found for ${archiveName}. Tried:`);
		for (const c of candidates) console.error(`  ${c}`);
		process.exit(1);
	}
	const binaryTarget = resolve(packageRoot, `apm${ext}`);
	fs.copyFileSync(binarySource, binaryTarget);
	fs.chmodSync(binaryTarget, 0o755);
	console.info(`Copied ${binarySource} -> ${binaryTarget}`);

	// Copy _internal directory (PyInstaller onedir runtime dependencies)
	const internalSource = resolve(binarySource, "..", "_internal");
	const internalTarget = resolve(packageRoot, "_internal");
	if (fs.existsSync(internalSource)) {
		if (fs.existsSync(internalTarget)) {
			fs.rmSync(internalTarget, { recursive: true });
		}
		fs.cpSync(internalSource, internalTarget, { recursive: true });
		console.info(`Copied ${internalSource} -> ${internalTarget}`);
	} else {
		console.warn(`_internal not found at ${internalSource}, skipping`);
	}

	// Copy README.md and LICENSE from repo root
	for (const fileName of ["README.md", "LICENSE"]) {
		const src = resolve(REPO_ROOT, fileName);
		const dest = resolve(packageRoot, fileName);
		if (fs.existsSync(src)) {
			fs.copyFileSync(src, dest);
			console.info(`Copied ${src} -> ${dest}`);
		} else {
			console.warn(`${fileName} not found at ${src}, skipping`);
		}
	}

}

/**
 * Updates the version in the `package.json` for the given `packageName` to
 * match the version specified in the `rootManifest`.
 */
function updateVersionInJsPackage(packageName) {
	const packageRoot = resolve(PACKAGES_ROOT, packageName);
	const manifestPath = resolve(packageRoot, "package.json");

	const { version } = rootManifest;

	const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf-8"));
	manifest.version = version;
	updateVersionInDependencies(manifest.dependencies, version);
	updateVersionInDependencies(manifest.devDependencies, version);
	updateVersionInDependencies(manifest.optionalDependencies, version);
	updateVersionInDependencies(
		manifest.peerDependencies,
		// Versions with a suffix shouldn't get the `^` prefix.
		version.includes("-") ? version : `^${version}`,
	);

	console.info(`Update manifest ${manifestPath}`);
	fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2));
}

function updateVersionInDependencies(dependencies, version) {
	if (dependencies) {
		for (const dependency of Object.keys(dependencies)) {
			if (dependency.startsWith("@apm/")) {
				dependencies[dependency] = version;
			}
		}
	}
}

// Platform packages: win32 only has x64 (no arm64 binary released yet).
const PACKAGES = [
	["win32", "x64"],
	["darwin", "x64"],
	["darwin", "arm64"],
	["linux", "x64"],
	["linux", "arm64"],
];

for (const [platform, arch] of PACKAGES) {
	copyBinaryToNativePackage(platform, arch);
}

updateVersionInJsPackage("apm");

// Copy README.md and LICENSE from repo root to the @apm/apm package directory
const APM_PACKAGE_ROOT = resolve(PACKAGES_ROOT, "apm");
for (const fileName of ["README.md", "LICENSE"]) {
	const src = resolve(REPO_ROOT, fileName);
	const dest = resolve(APM_PACKAGE_ROOT, fileName);
	if (fs.existsSync(src)) {
		fs.copyFileSync(src, dest);
		console.info(`Copied ${src} -> ${dest}`);
	} else {
		console.warn(`${fileName} not found at ${src}, skipping`);
	}
}



