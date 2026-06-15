/**
 * Shared utilities for abx plugins (JavaScript).
 *
 * Provides common helpers used across multiple plugins:
 * - Environment variable parsing (getEnv, getEnvBool, getEnvInt, getEnvArray)
 * - CLI argument parsing (parseArgs)
 * - JSONL record emission (emitArchiveResultRecord, emitSnapshotRecord)
 * - Atomic file writing (writeFileAtomic)
 * - Sibling plugin output checking (hasStaticFileOutput)
 */

const fs = require('fs');
const os = require('os');
const path = require('path');

const BASE_CONFIG_PATH = path.join(__dirname, 'config.json');
const PROCESS_EXIT_SKIPPED = 10;
const configCache = new Map();

function fsyncIfRegularFile(fd) {
    try {
        const stats = fs.fstatSync(fd);
        if (stats.isFile()) {
            fs.fsyncSync(fd);
        }
    } catch (error) {
        return;
    }
}

function writeFdFully(fd, text) {
    const buffer = Buffer.from(text, 'utf8');
    let offset = 0;
    while (offset < buffer.length) {
        offset += fs.writeSync(fd, buffer, offset, buffer.length - offset);
    }
    fsyncIfRegularFile(fd);
}

// ---------------------------------------------------------------------------
// Environment variable helpers
// ---------------------------------------------------------------------------

function getCallerFile(skipFn) {
    const original = Error.prepareStackTrace;
    try {
        Error.prepareStackTrace = (_, stack) => stack;
        const err = new Error();
        Error.captureStackTrace(err, skipFn);
        const stack = err.stack || [];
        for (const site of stack) {
            const fileName = site && typeof site.getFileName === 'function' ? site.getFileName() : null;
            if (fileName && fileName !== __filename) {
                return fileName;
            }
        }
    } finally {
        Error.prepareStackTrace = original;
    }
    return null;
}

function normalizeSchemaType(prop = {}) {
    const rawType = prop.type || 'string';
    if (Array.isArray(rawType)) {
        const nonNullTypes = rawType.filter(typeName => typeName !== 'null');
        return {
            schemaType: nonNullTypes[0] || 'string',
            nullable: rawType.includes('null'),
        };
    }
    return {
        schemaType: rawType,
        nullable: false,
    };
}

function parseArrayValue(rawValue, defaultValue) {
    const trimmed = String(rawValue || '').trim();
    if (!trimmed) return defaultValue;
    if (trimmed.startsWith('[')) {
        try {
            const parsed = JSON.parse(trimmed);
            if (Array.isArray(parsed)) return parsed.map(item => String(item));
        } catch (error) {}
    }
    return trimmed.split(',').map(item => item.trim()).filter(Boolean);
}

function getPlatformUserConfigDir() {
    if (process.platform === 'darwin') {
        return path.join(os.homedir(), 'Library', 'Application Support', 'abx');
    }
    if (process.platform === 'win32') {
        return path.join(process.env.APPDATA || path.join(os.homedir(), 'AppData', 'Roaming'), 'abx');
    }
    return path.join(process.env.XDG_CONFIG_HOME || path.join(os.homedir(), '.config'), 'abx');
}

function parseConfigValue(rawValue, prop = {}) {
    const { schemaType, nullable } = normalizeSchemaType(prop);
    const defaultValue = Object.prototype.hasOwnProperty.call(prop, 'default')
        ? prop.default
        : (nullable ? null : undefined);

    if (rawValue === undefined) {
        return defaultValue;
    }

    const trimmed = String(rawValue).trim();
    if (nullable && trimmed === '') {
        return null;
    }

    if (schemaType === 'boolean') {
        const lowered = trimmed.toLowerCase();
        if (['true', '1', 'yes', 'on'].includes(lowered)) return true;
        if (['false', '0', 'no', 'off'].includes(lowered)) return false;
        return defaultValue;
    }

    if (schemaType === 'integer') {
        const parsed = parseInt(trimmed, 10);
        return Number.isNaN(parsed) ? defaultValue : parsed;
    }

    if (schemaType === 'number') {
        const parsed = Number(trimmed);
        return Number.isNaN(parsed) ? defaultValue : parsed;
    }

    if (schemaType === 'array') {
        return parseArrayValue(trimmed, defaultValue || []);
    }

    return trimmed;
}

function resolveConfigPath(configPath = null) {
    if (configPath) return path.resolve(configPath);
    const callerFile = getCallerFile(loadConfig);
    if (!callerFile) return BASE_CONFIG_PATH;
    return path.join(path.dirname(path.resolve(callerFile)), 'config.json');
}

function loadConfig(configPath = null) {
    const pluginConfigPath = resolveConfigPath(configPath);
    const pluginMtime = fs.statSync(pluginConfigPath).mtimeMs;
    const baseMtime = fs.statSync(BASE_CONFIG_PATH).mtimeMs;
    const cacheKey = pluginConfigPath;
    const cached = configCache.get(cacheKey);
    if (cached && cached.pluginMtime === pluginMtime && cached.baseMtime === baseMtime) {
        return { ...cached.config };
    }

    const baseSchema = JSON.parse(fs.readFileSync(BASE_CONFIG_PATH, 'utf8'));
    const pluginSchema = pluginConfigPath === BASE_CONFIG_PATH
        ? baseSchema
        : JSON.parse(fs.readFileSync(pluginConfigPath, 'utf8'));

    const properties = pluginConfigPath === BASE_CONFIG_PATH
        ? (baseSchema.properties || {})
        : { ...(baseSchema.properties || {}), ...(pluginSchema.properties || {}) };

    const config = {};
    for (const [name, prop] of Object.entries(properties)) {
        const choices = [name, ...(prop['x-aliases'] || [])];
        if (prop['x-fallback']) {
            choices.push(prop['x-fallback']);
        }

        let rawValue;
        for (const choice of choices) {
            if (Object.prototype.hasOwnProperty.call(process.env, choice)) {
                rawValue = process.env[choice];
                break;
            }
        }

        config[name] = parseConfigValue(rawValue, prop);
    }

    if (!config.PERSONAS_DIR) {
        config.PERSONAS_DIR = path.join(getPlatformUserConfigDir(), 'personas');
    }

    configCache.set(cacheKey, {
        config,
        pluginMtime,
        baseMtime,
    });
    return { ...config };
}

function getConfig(configPath = null) {
    return loadConfig(configPath);
}

function getEnv(name, defaultValue = '') {
    return (process.env[name] || defaultValue).trim();
}

function getEnvBool(name, defaultValue = false) {
    const val = getEnv(name, '').toLowerCase();
    if (['true', '1', 'yes', 'on'].includes(val)) return true;
    if (['false', '0', 'no', 'off'].includes(val)) return false;
    return defaultValue;
}

function getEnvInt(name, defaultValue = 0) {
    const val = parseInt(getEnv(name, String(defaultValue)), 10);
    return isNaN(val) ? defaultValue : val;
}

/**
 * Get array environment variable (JSON array or comma-separated string).
 *
 * If value starts with '[', parse as JSON array.
 * Otherwise, parse as comma-separated values.
 */
function getEnvArray(name, defaultValue = []) {
    const val = getEnv(name, '');
    if (!val) return defaultValue;

    if (val.startsWith('[')) {
        try {
            const parsed = JSON.parse(val);
            if (Array.isArray(parsed)) return parsed;
        } catch (e) {
            // Warn when a value looks like JSON but fails to parse, then
            // fall through to comma-separated parsing below.
            writeFdFully(2, `[base/utils.js] Warning: ${name} looks like JSON but failed to parse: ${e.message}\n`);
        }
    }

    return val.split(',').map(s => s.trim()).filter(Boolean);
}

function getLibDir() {
    const configured = (loadConfig(BASE_CONFIG_PATH).ABXPKG_LIB_DIR || '').trim();
    if (configured) return path.resolve(configured);
    return path.resolve(path.join(getPlatformUserConfigDir(), 'lib'));
}

function getNodeModulesDir() {
    const configured = getEnv('NODE_MODULES_DIR');
    if (!configured) {
        throw new Error('NODE_MODULES_DIR is required; run hooks through abxpkg/abx-dl/archivebox so provider env is resolved once and passed to the hook');
    }
    return path.resolve(configured);
}

function ensureNodeModuleResolution(moduleRef = module) {
    const nodeModulesDir = getNodeModulesDir();

    if (!process.env.NODE_MODULES_DIR && process.env.NODE_MODULE_DIR) {
        process.env.NODE_MODULES_DIR = process.env.NODE_MODULE_DIR;
    }
    if (!process.env.NODE_MODULE_DIR && process.env.NODE_MODULES_DIR) {
        process.env.NODE_MODULE_DIR = process.env.NODE_MODULES_DIR;
    }
    if (!process.env.NODE_PATH) {
        process.env.NODE_PATH = nodeModulesDir;
    }

    if (!moduleRef.paths.includes(nodeModulesDir)) {
        moduleRef.paths.unshift(nodeModulesDir);
    }

    return nodeModulesDir;
}

// ---------------------------------------------------------------------------
// CLI argument parsing
// ---------------------------------------------------------------------------

/**
 * Parse --key=value arguments from process.argv.
 * Returns an object with keys (dashes converted to underscores).
 */
function parseArgs() {
    const args = {};
    process.argv.slice(2).forEach((arg) => {
        if (arg.startsWith('--')) {
            const [key, ...valueParts] = arg.slice(2).split('=');
            args[key.replace(/-/g, '_')] = valueParts.join('=') || true;
        }
    });
    return args;
}

// ---------------------------------------------------------------------------
// JSONL record emission
// ---------------------------------------------------------------------------

function parseExtraContext(raw, source) {
    try {
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
            return parsed;
        }
        writeFdFully(2, `[base/utils.js] Warning: ignoring non-object extra context from ${source}\n`);
    } catch (error) {
        writeFdFully(2, `[base/utils.js] Warning: ignoring invalid extra context from ${source}: ${error.message}\n`);
    }
    return {};
}

function getExtraContext() {
    const context = {};
    const envRaw = (loadConfig(BASE_CONFIG_PATH).EXTRA_CONTEXT || '').trim();
    if (envRaw) {
        Object.assign(context, parseExtraContext(envRaw, 'EXTRA_CONTEXT'));
    }

    const argv = process.argv.slice(2);
    for (let i = 0; i < argv.length; i += 1) {
        const arg = argv[i];
        if (arg === '--extra-context') {
            const value = argv[i + 1];
            if (value === undefined) {
                writeFdFully(2, '[base/utils.js] Warning: ignoring missing value for --extra-context\n');
                return context;
            }
            Object.assign(context, parseExtraContext(value, '--extra-context'));
            return context;
        }
        if (arg.startsWith('--extra-context=')) {
            Object.assign(context, parseExtraContext(arg.slice('--extra-context='.length), '--extra-context'));
            return context;
        }
    }

    return context;
}

function mergeExtraContext(record) {
    const extraContext = getExtraContext();
    if (!Object.keys(extraContext).length) {
        return record;
    }
    return { ...extraContext, ...record };
}

function emitArchiveResultRecord(status, outputStr, extra = {}) {
    writeFdFully(1, `${JSON.stringify(mergeExtraContext({
        type: 'ArchiveResult',
        status,
        output_str: outputStr,
        ...extra,
    }))}\n`);
}

function emitSnapshotRecord(record) {
    const snapshotRecord = mergeExtraContext({
        type: 'Snapshot',
        ...record,
    });
    snapshotRecord.id = snapshotRecord.id ? String(snapshotRecord.id) : '';
    writeFdFully(1, `${JSON.stringify(snapshotRecord)}\n`);
}

// ---------------------------------------------------------------------------
// Atomic file writing
// ---------------------------------------------------------------------------

function writeFileAtomic(filePath, contents) {
    const dir = path.dirname(filePath);
    const base = path.basename(filePath);
    const tmpPath = path.join(dir, `.${base}.${process.pid}.tmp`);
    fs.writeFileSync(tmpPath, contents, 'utf8');
    fs.renameSync(tmpPath, filePath);
}

// ---------------------------------------------------------------------------
// Sibling plugin output checking
// ---------------------------------------------------------------------------

function hasStaticFileOutput(staticfileDir = '../staticfile') {
    if (!fs.existsSync(staticfileDir)) return false;
    const stdoutPath = path.join(staticfileDir, 'stdout.log');
    if (!fs.existsSync(stdoutPath)) return false;
    const stdout = fs.readFileSync(stdoutPath, 'utf8');
    for (const line of stdout.split('\n')) {
        const trimmed = line.trim();
        if (!trimmed.startsWith('{')) continue;
        try {
            const record = JSON.parse(trimmed);
            if (record.type === 'ArchiveResult' && record.status === 'succeeded') {
                return true;
            }
        } catch (e) {}
    }
    return false;
}

module.exports = {
    PROCESS_EXIT_SKIPPED,
    getConfig,
    loadConfig,
    getEnv,
    getEnvBool,
    getEnvInt,
    getEnvArray,
    getExtraContext,
    getPlatformUserConfigDir,
    getLibDir,
    getNodeModulesDir,
    ensureNodeModuleResolution,
    parseArgs,
    emitArchiveResultRecord,
    emitSnapshotRecord,
    writeFileAtomic,
    hasStaticFileOutput,
};
