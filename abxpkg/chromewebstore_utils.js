/**
 * Chrome Web Store extension installer for ChromeWebstoreProvider.
 *
 * Keep this helper intentionally narrow: it only downloads, unpacks, sanitizes,
 * and caches Web Store CRX payloads for abxpkg.
 */

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { execFile } = require('child_process');
const { promisify } = require('util');
const { Readable } = require('stream');
const { finished } = require('stream/promises');

const execFileAsync = promisify(execFile);

function makeTreeWritable(targetPath) {
    if (!fs.existsSync(targetPath)) return;

    const stat = fs.lstatSync(targetPath);
    fs.chmodSync(targetPath, stat.mode | 0o700);
    if (!stat.isDirectory()) return;

    for (const entry of fs.readdirSync(targetPath)) {
        makeTreeWritable(path.join(targetPath, entry));
    }
}

/**
 * Compute the extension ID Chrome derives for an unpacked extension path.
 *
 * Chrome hashes the real unpacked extension directory path, then maps the first
 * 32 hex chars into the a-p alphabet. Keeping the unpacked path stable keeps
 * the generated extension ID stable across browser launches.
 */
function getExtensionId(unpackedPath) {
    let resolvedPath = unpackedPath;
    try {
        resolvedPath = fs.realpathSync(unpackedPath);
    } catch (err) {
        resolvedPath = unpackedPath;
    }

    const hash = crypto.createHash('sha256');
    hash.update(Buffer.from(resolvedPath, 'utf-8'));

    return Array.from(hash.digest('hex'))
        .slice(0, 32)
        .map(i => String.fromCharCode(parseInt(i, 16) + 'a'.charCodeAt(0)))
        .join('');
}

async function sanitizeUnpackedExtension(unpackedPath) {
    // Store CRX payloads include signed-install metadata that Chromium rejects
    // when loading the same files as an unpacked extension.
    makeTreeWritable(unpackedPath);
    await fs.promises.rm(path.join(unpackedPath, '_metadata'), {
        recursive: true,
        force: true,
    });
}

async function installExtension(extension, options = {}) {
    const { forceInstall = false, unzipPath } = options;
    if (!unzipPath) {
        throw new Error('unzip_path is required');
    }
    const manifestPath = path.join(extension.unpacked_path, 'manifest.json');

    if (forceInstall || (!fs.existsSync(manifestPath) && !fs.existsSync(extension.crx_path))) {
        console.log(`[🛠️] Downloading missing extension ${extension.name} ${extension.webstore_id} -> ${extension.crx_path}`);

        const crxDir = path.dirname(extension.crx_path);
        await fs.promises.mkdir(crxDir, { recursive: true });

        try {
            const response = await fetch(extension.crx_url);
            if (!response.ok) {
                console.warn(`[⚠️] Failed to download extension ${extension.name}: HTTP ${response.status}`);
                return false;
            }
            if (!response.body) {
                console.warn(`[⚠️] Failed to download extension ${extension.name}: No response body`);
                return false;
            }
            const crxFile = fs.createWriteStream(extension.crx_path);
            const crxStream = Readable.fromWeb(response.body);
            await finished(crxStream.pipe(crxFile));
        } catch (err) {
            fs.rmSync(extension.crx_path, { force: true });
            console.error(`[❌] Failed to download extension ${extension.name}:`, err);
            return false;
        }
    }

    makeTreeWritable(extension.unpacked_path);
    await fs.promises.mkdir(extension.unpacked_path, { recursive: true });

    try {
        await execFileAsync(unzipPath, [
            '-q',
            '-o',
            extension.crx_path,
            '-d',
            extension.unpacked_path,
        ]);
    } catch (err) {
        // unzip can return non-zero on CRX header warnings. The manifest is the
        // real success condition for the unpacked extension payload.
        if (!fs.existsSync(manifestPath)) {
            console.error(`[❌] Failed to unzip ${extension.crx_path}:`, err.message);
            return false;
        }
    }

    if (!fs.existsSync(manifestPath)) {
        console.error(`[❌] Failed to install ${extension.crx_path}: could not find manifest.json in unpacked_path`);
        return false;
    }

    await sanitizeUnpackedExtension(extension.unpacked_path);
    return true;
}

async function loadOrInstallExtension(ext, extensionsDir, unzipPath, forceInstall = false) {
    if (!(ext.webstore_id || ext.unpacked_path)) {
        throw new Error('Extension must have either {webstore_id} or {unpacked_path}');
    }
    if (!extensionsDir) {
        throw new Error('extensions_dir is required');
    }

    ext.webstore_id = ext.webstore_id || ext.id;
    ext.name = ext.name || ext.webstore_id;
    ext.webstore_url = ext.webstore_url || `https://chromewebstore.google.com/detail/${ext.webstore_id}`;
    ext.crx_url = ext.crx_url || `https://clients2.google.com/service/update2/crx?response=redirect&prodversion=1230&acceptformat=crx3&x=id%3D${ext.webstore_id}%26uc`;
    ext.crx_path = ext.crx_path || path.join(extensionsDir, `${ext.webstore_id}__${ext.name}.crx`);
    ext.unpacked_path = ext.unpacked_path || path.join(extensionsDir, `${ext.webstore_id}__${ext.name}`);

    const manifestPath = path.join(ext.unpacked_path, 'manifest.json');
    ext.read_manifest = () => JSON.parse(fs.readFileSync(manifestPath, 'utf-8'));
    ext.read_version = () => fs.existsSync(manifestPath) && ext.read_manifest()?.version || null;

    if (forceInstall || !ext.read_version()) {
        await installExtension(ext, { forceInstall, unzipPath });
    }
    await sanitizeUnpackedExtension(ext.unpacked_path);

    ext.id = getExtensionId(ext.unpacked_path);
    ext.version = ext.read_version();

    delete ext.read_manifest;
    delete ext.read_version;

    if (!ext.version) {
        console.warn(`[❌] Unable to detect ID and version of installed extension ${ext.unpacked_path}`);
    } else {
        console.log(`[➕] Installed extension ${ext.name} (${ext.version})... ${ext.unpacked_path}`);
    }

    return ext;
}

async function installExtensionWithCache(extension, options = {}) {
    const {
        extensionsDir,
        unzipPath,
        quiet = false,
        noCache = false,
    } = options;

    if (!extensionsDir) {
        throw new Error('extensions_dir is required');
    }
    if (!unzipPath) {
        throw new Error('unzip_path is required');
    }

    const cacheFile = path.join(extensionsDir, `${extension.name}.extension.json`);

    if (!noCache && fs.existsSync(cacheFile)) {
        try {
            const cached = JSON.parse(fs.readFileSync(cacheFile, 'utf-8'));
            const manifestPath = path.join(cached.unpacked_path, 'manifest.json');

            if (cached.webstore_id === extension.webstore_id && fs.existsSync(manifestPath)) {
                await sanitizeUnpackedExtension(cached.unpacked_path);
                if (!quiet) {
                    console.log(`[*] ${extension.name} extension already installed (using cache)`);
                }
                return cached;
            }
        } catch (err) {
            console.warn(`[⚠️] Extension cache corrupted for ${extension.name}, re-installing...`);
        }
    }

    if (!quiet) {
        console.log(`[*] Installing ${extension.name} extension...`);
    }

    const installedExt = await loadOrInstallExtension(extension, extensionsDir, unzipPath, noCache);

    if (!installedExt?.version) {
        console.error(`[❌] Failed to install ${extension.name} extension`);
        return null;
    }

    await fs.promises.mkdir(extensionsDir, { recursive: true });
    await fs.promises.writeFile(cacheFile, JSON.stringify(installedExt, null, 2));

    if (!quiet) {
        console.log(`[+] Extension metadata written to ${cacheFile}`);
        console.log(`[+] ${extension.name} extension installed`);
    }

    return installedExt;
}

async function main() {
    const [command, ...args] = process.argv.slice(2);

    switch (command) {
        case 'getExtensionId': {
            const [unpackedPath] = args;
            if (!unpackedPath) {
                console.error('Usage: getExtensionId <path>');
                process.exit(1);
            }
            console.log(getExtensionId(unpackedPath));
            break;
        }

        case 'installExtensionWithCache': {
            const [webstoreId, name, extensionsDir, unzipPath, maybeNoCache] = args;
            if (!webstoreId || !name) {
                console.error('Usage: installExtensionWithCache <webstore_id> <name> <extensions_dir> <unzip_path> [--no-cache]');
                process.exit(1);
            }
            const noCache = maybeNoCache === '--no-cache';
            const ext = await installExtensionWithCache(
                { webstore_id: webstoreId, name },
                { extensionsDir, unzipPath, noCache },
            );
            if (ext) {
                console.log(JSON.stringify(ext, null, 2));
            } else {
                process.exit(1);
            }
            break;
        }

        default:
            console.error('Usage: chromewebstore_utils.js <command> [args...]');
            console.error('  getExtensionId <path>');
            console.error('  installExtensionWithCache <webstore_id> <name> <extensions_dir> <unzip_path> [--no-cache]');
            process.exit(1);
    }
}

if (require.main === module) {
    main().catch(err => {
        console.error(err);
        process.exit(1);
    });
}

module.exports = {
    getExtensionId,
    installExtension,
    installExtensionWithCache,
    loadOrInstallExtension,
    sanitizeUnpackedExtension,
};
