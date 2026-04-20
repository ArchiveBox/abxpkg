#!/usr/bin/env node
/**
 * Chrome Extension Management Utilities
 *
 * Handles downloading, installing, and managing Chrome extensions for browser automation.
 * Ported from the TypeScript implementation in archivebox.ts
 */

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const http = require('http');
const os = require('os');
const net = require('net');
const { exec, spawn } = require('child_process');
const { promisify } = require('util');
const { Readable } = require('stream');
const { finished } = require('stream/promises');

const execAsync = promisify(exec);

// Import generic helpers from base plugin
const {
    getEnv,
    getEnvBool,
    getEnvInt,
    getEnvArray,
    getNodeModulesDir: getNodeModulesDirFromBaseUtils,
    ensureNodeModuleResolution,
    loadConfig,
    parseArgs,
} = require('../base/utils.js');

ensureNodeModuleResolution(module);

const CHROME_SESSION_REQUIRED_ERROR = 'No Chrome session found (chrome plugin must run first)';

/**
 * Get the current snapshot directory.
 * Priority: SNAP_DIR, or cwd.
 *
 * @returns {string} - Absolute path to snapshot directory
 */
function getSnapDir() {
    const snapDir = getEnv('SNAP_DIR');
    if (snapDir) return path.resolve(snapDir);
    return path.resolve(process.cwd());
}

/**
 * Get the current crawl directory.
 * Priority: CRAWL_DIR, or cwd.
 *
 * @returns {string} - Absolute path to crawl directory
 */
function getCrawlDir() {
    const crawlDir = getEnv('CRAWL_DIR');
    if (crawlDir) return path.resolve(crawlDir);
    return path.resolve(process.cwd());
}

/**
 * Get the personas directory.
 * Returns the configured personas directory from loadConfig().
 *
 * @returns {string} - Absolute path to personas directory
 */
function getPersonasDir() {
    const config = loadConfig(path.join(__dirname, 'config.json'));
    return path.resolve(config.PERSONAS_DIR);
}

/**
 * Parse resolution string into width/height.
 * @param {string} resolution - Resolution string like "1440,2000"
 * @returns {{width: number, height: number}} - Parsed dimensions
 */
function parseResolution(resolution) {
    const [width, height] = resolution.split(',').map(x => parseInt(x.trim(), 10));
    return { width: width || 1440, height: height || 2000 };
}

// ============================================================================
// PID file management
// ============================================================================

/**
 * Write PID file with specific mtime for process validation.
 * @param {string} filePath - Path to PID file
 * @param {number} pid - Process ID
 * @param {number} startTimeSeconds - Process start time in seconds
 */
function writePidWithMtime(filePath, pid, startTimeSeconds) {
    fs.writeFileSync(filePath, String(pid));
    const startTimeMs = startTimeSeconds * 1000;
    fs.utimesSync(filePath, new Date(startTimeMs), new Date(startTimeMs));
}

/**
 * Write a shell script that can re-run the Chrome command.
 * @param {string} filePath - Path to script file
 * @param {string} binary - Chrome binary path
 * @param {string[]} args - Chrome arguments
 */
function writeCmdScript(filePath, binary, args) {
    const escape = (arg) =>
        arg.includes(' ') || arg.includes('"') || arg.includes('$')
            ? `"${arg.replace(/"/g, '\\"')}"`
            : arg;
    fs.writeFileSync(
        filePath,
        `#!/bin/bash\n${binary} ${args.map(escape).join(' ')}\n`
    );
    fs.chmodSync(filePath, 0o755);
}

// ============================================================================
// Port management
// ============================================================================

/**
 * Find a free port on localhost.
 * @returns {Promise<number>} - Available port number
 */
function findFreePort() {
    return new Promise((resolve, reject) => {
        const server = net.createServer();
        server.unref();
        server.on('error', reject);
        server.listen(0, () => {
            const port = server.address().port;
            server.close(() => resolve(port));
        });
    });
}

/**
 * Wait for Chrome's DevTools port to be ready.
 * @param {number} port - Debug port number
 * @param {number} [timeout=30000] - Timeout in milliseconds
 * @returns {Promise<Object>} - Chrome version info
 */
function waitForDebugPort(port, timeout = 30000) {
    const startTime = Date.now();
    let lastFailure = 'no response yet';
    const hosts = ['127.0.0.1', '::1', 'localhost'];

    const normalizeWsUrl = (rawWsUrl) => {
        try {
            const parsed = new URL(rawWsUrl);
            if (!parsed.port) parsed.port = String(port);
            return parsed.toString();
        } catch (e) {
            return rawWsUrl;
        }
    };

    const probeDebugPort = (host) => new Promise((resolve, reject) => {
        const req = http.request(
            {
                host,
                port,
                path: '/json/version',
                method: 'GET',
                headers: {
                    Host: `${host}:${port}`,
                    Connection: 'close',
                },
                timeout: 5000,
            },
            (res) => {
                let data = '';
                res.on('data', (chunk) => (data += chunk));
                res.on('end', () => {
                    if ((res.statusCode || 0) >= 400) {
                        reject(new Error(`HTTP ${res.statusCode}`));
                        return;
                    }
                    try {
                        const info = JSON.parse(data);
                        if (!info?.webSocketDebuggerUrl) {
                            reject(new Error('missing webSocketDebuggerUrl in /json/version response'));
                            return;
                        }
                        info.webSocketDebuggerUrl = normalizeWsUrl(info.webSocketDebuggerUrl);
                        resolve(info);
                    } catch (error) {
                        reject(new Error(`invalid /json/version payload: ${error.message}`));
                    }
                });
            }
        );
        req.on('error', reject);
        req.on('timeout', () => {
            req.destroy(new Error('request timeout'));
        });
        req.end();
    });

    return new Promise((resolve, reject) => {
        const tryConnect = async () => {
            if (Date.now() - startTime > timeout) {
                reject(new Error(`Timeout waiting for Chrome debug port ${port} (${lastFailure})`));
                return;
            }

            for (const host of hosts) {
                try {
                    const info = await probeDebugPort(host);
                    resolve(info);
                    return;
                } catch (error) {
                    lastFailure = `${host}: ${error.message}`;
                }
            }

            setTimeout(tryConnect, 100);
        };

        tryConnect();
    });
}

// ============================================================================
// Zombie process cleanup
// ============================================================================

/**
 * Kill zombie Chrome processes from stale crawls.
 * Recursively scans SNAP_DIR for any .../chrome/chrome.pid files whose owning
 * crawl no longer has a live ``.heartbeat.json`` lease.
 * @param {string} [snapDir] - Snapshot directory (defaults to SNAP_DIR env or cwd)
 * @param {Object} [options={}] - Cleanup options
 * @param {string[]} [options.excludeCrawlDirs=[]] - Crawl directories to never treat as stale
 * @param {boolean} [options.excludeCurrentRuntimeDirs=true] - Whether to auto-skip the current CRAWL_DIR/SNAP_DIR
 * @returns {number} - Number of zombies killed
 */
async function killZombieChrome(snapDir = null, options = {}) {
    snapDir = snapDir || getSnapDir();
    let killed = 0;
    const currentPid = process.pid;
    const quiet = Boolean(options.quiet);
    const excludeCurrentRuntimeDirs = options.excludeCurrentRuntimeDirs !== false;
    const excludeCrawlDirs = new Set(
        (options.excludeCrawlDirs || []).map(dir => path.resolve(dir))
    );
    const excludeSessionDirs = new Set(
        (options.excludeSessionDirs || []).map(dir => path.resolve(dir))
    );
    if (excludeCurrentRuntimeDirs) {
        excludeSessionDirs.add(path.resolve(getSnapDir()));
        excludeSessionDirs.add(path.resolve(getCrawlDir()));
    }

    if (!quiet) console.error('[*] Checking for zombie Chrome processes...');

    if (!fs.existsSync(snapDir)) {
        if (!quiet) console.error('[+] No snapshot directory found');
        return 0;
    }

    /**
     * Recursively find all chrome/chrome.pid files in directory tree
     * @param {string} dir - Directory to search
     * @param {number} depth - Current recursion depth (limit to 10)
     * @returns {Array<{pidFile: string, chromeDir: string, sessionDir: string}>} - Array of PID file info
     */
    function findChromePidFiles(dir, depth = 0) {
        if (depth > 10) return [];  // Prevent infinite recursion

        const results = [];
        try {
            const entries = fs.readdirSync(dir, { withFileTypes: true });

            for (const entry of entries) {
                if (!entry.isDirectory()) continue;

                const fullPath = path.join(dir, entry.name);

                // Found a chrome directory - only consider the shared browser marker.
                if (entry.name === 'chrome') {
                    try {
                        const crawlDir = dir;  // Parent of chrome/ is the crawl dir
                        const chromePidFile = path.join(fullPath, 'chrome.pid');
                        if (fs.existsSync(chromePidFile)) {
                            results.push({
                                pidFile: chromePidFile,
                                chromeDir: fullPath,
                                sessionDir: crawlDir,
                            });
                        }
                    } catch (e) {
                        // Skip if can't read chrome dir
                    }
                } else {
                    // Recurse into subdirectory (skip hidden dirs and node_modules)
                    if (!entry.name.startsWith('.') && entry.name !== 'node_modules') {
                        results.push(...findChromePidFiles(fullPath, depth + 1));
                    }
                }
            }
        } catch (e) {
            // Skip if can't read directory
        }
        return results;
    }

    function findOwningCrawlDir(sessionDir) {
        let currentDir = path.resolve(sessionDir);
        const rootDir = path.resolve(snapDir);
        while (currentDir.startsWith(rootDir)) {
            if (fs.existsSync(path.join(currentDir, '.heartbeat.json'))) {
                return currentDir;
            }
            if (excludeCrawlDirs.has(currentDir) || excludeSessionDirs.has(currentDir)) {
                return currentDir;
            }
            const parentDir = path.dirname(currentDir);
            if (parentDir === currentDir) {
                break;
            }
            currentDir = parentDir;
        }
        return path.resolve(sessionDir);
    }

    function crawlHeartbeatIsAlive(crawlDir) {
        const heartbeatFile = path.join(crawlDir, '.heartbeat.json');
        try {
            const heartbeat = JSON.parse(fs.readFileSync(heartbeatFile, 'utf8'));
            const ownerPid = parseInt(String(heartbeat.owner_pid), 10);
            const lastAliveAt = Number(heartbeat.last_alive_at);
            const killAfterSeconds = Number(heartbeat.kill_after_seconds || 180);
            if (isNaN(ownerPid) || ownerPid <= 0 || !Number.isFinite(lastAliveAt)) {
                return false;
            }
            if (!isProcessAlive(ownerPid)) {
                return false;
            }
            return (Date.now() / 1000) - lastAliveAt <= killAfterSeconds;
        } catch (error) {
            return false;
        }
    }

    function getHeartbeatOwnerPid(crawlDir) {
        const heartbeatFile = path.join(crawlDir, '.heartbeat.json');
        try {
            const heartbeat = JSON.parse(fs.readFileSync(heartbeatFile, 'utf8'));
            const ownerPid = parseInt(String(heartbeat.owner_pid), 10);
            return Number.isNaN(ownerPid) || ownerPid <= 0 ? null : ownerPid;
        } catch (error) {
            return null;
        }
    }

    function findChromeHookPidFiles(dir, depth = 0) {
        if (depth > 10) return [];

        const results = [];
        try {
            const entries = fs.readdirSync(dir, { withFileTypes: true });
            for (const entry of entries) {
                if (!entry.isDirectory()) continue;
                const fullPath = path.join(dir, entry.name);
                if (entry.name === 'chrome') {
                    try {
                        const crawlDir = dir;
                        for (const chromeEntry of fs.readdirSync(fullPath, { withFileTypes: true })) {
                            if (!chromeEntry.isFile()) continue;
                            if (!chromeEntry.name.endsWith('.pid')) continue;
                            if (chromeEntry.name === 'chrome.pid') continue;
                            if (!chromeEntry.name.startsWith('on_')) continue;
                            if (!chromeEntry.name.includes('chrome_')) continue;
                            const pidFile = path.join(fullPath, chromeEntry.name);
                            results.push({
                                pidFile,
                                hookName: chromeEntry.name.slice(0, -4),
                                chromeDir: fullPath,
                                sessionDir: crawlDir,
                            });
                        }
                    } catch (error) {
                        // Skip unreadable chrome directories
                    }
                } else if (!entry.name.startsWith('.') && entry.name !== 'node_modules') {
                    results.push(...findChromeHookPidFiles(fullPath, depth + 1));
                }
            }
        } catch (error) {
            // Skip unreadable directories
        }
        return results;
    }

    function getParentPid(pid) {
        try {
            const { execSync } = require('child_process');
            const output = execSync(`ps -o ppid= -p ${pid}`, {
                encoding: 'utf8',
                timeout: 5000,
                stdio: ['ignore', 'pipe', 'ignore'],
            }).trim();
            const parentPid = parseInt(output, 10);
            return Number.isNaN(parentPid) || parentPid <= 0 ? null : parentPid;
        } catch (error) {
            return null;
        }
    }

    function processHasAncestorPid(pid, ancestorPid) {
        if (!ancestorPid || !isProcessAlive(ancestorPid)) {
            return false;
        }
        const seen = new Set();
        let currentPid = pid;
        while (currentPid && !seen.has(currentPid)) {
            if (currentPid === ancestorPid) {
                return true;
            }
            seen.add(currentPid);
            currentPid = getParentPid(currentPid);
        }
        return false;
    }

    function getProcessCommand(pid) {
        try {
            const { execSync } = require('child_process');
            return execSync(`ps -o command= -p ${pid}`, {
                encoding: 'utf8',
                timeout: 5000,
                stdio: ['ignore', 'pipe', 'ignore'],
            }).trim();
        } catch (error) {
            return '';
        }
    }

    function getProcessWorkingDir(pid) {
        try {
            const { execSync } = require('child_process');
            const output = execSync(`lsof -a -p ${pid} -d cwd -Fn`, {
                encoding: 'utf8',
                timeout: 5000,
                stdio: ['ignore', 'pipe', 'ignore'],
            });
            for (const line of output.split('\n')) {
                if (line.startsWith('n')) {
                    return path.resolve(line.slice(1).trim());
                }
            }
        } catch (error) {
            return null;
        }
        return null;
    }

    function findChromeHookProcesses() {
        try {
            const { execSync } = require('child_process');
            const output = execSync('ps -axo pid=,command=', { encoding: 'utf8', timeout: 5000 });
            const hookMatches = [];
            for (const line of output.split('\n')) {
                const trimmed = line.trim();
                if (!trimmed) continue;
                const match = trimmed.match(/^(\d+)\s+(.*)$/);
                if (!match) continue;
                const pid = parseInt(match[1], 10);
                const command = match[2];
                if (Number.isNaN(pid) || pid <= 0) continue;
                if (command.includes('on_CrawlSetup__90_chrome_launch.daemon.bg.js')) {
                    hookMatches.push({ pid, hookName: 'on_CrawlSetup__90_chrome_launch.daemon.bg' });
                    continue;
                }
                if (command.includes('on_Snapshot__09_chrome_launch.daemon.bg.js')) {
                    hookMatches.push({ pid, hookName: 'on_Snapshot__09_chrome_launch.daemon.bg' });
                    continue;
                }
                if (command.includes('on_Snapshot__10_chrome_tab.daemon.bg.js')) {
                    hookMatches.push({ pid, hookName: 'on_Snapshot__10_chrome_tab.daemon.bg' });
                }
            }
            return hookMatches;
        } catch (error) {
            return [];
        }
    }

    async function killHookProcess(pid, expectedHookName) {
        const currentCommand = getProcessCommand(pid);
        if (!currentCommand || !currentCommand.includes(expectedHookName)) {
            return false;
        }

        try {
            process.kill(pid, 'SIGTERM');
        } catch (error) {
            if (error.code !== 'ESRCH') {
                console.error(`[!] Failed to SIGTERM hook PID ${pid}: ${error.message}`);
            }
        }

        const deadline = Date.now() + 5000;
        while (Date.now() < deadline) {
            if (!isProcessAlive(pid)) {
                return true;
            }
            await sleep(200);
        }

        if (isProcessAlive(pid)) {
            try {
                process.kill(pid, 'SIGKILL');
            } catch (error) {
                if (error.code !== 'ESRCH') {
                    console.error(`[!] Failed to SIGKILL hook PID ${pid}: ${error.message}`);
                }
            }
        }

        const killDeadline = Date.now() + 5000;
        while (Date.now() < killDeadline) {
            if (!isProcessAlive(pid)) {
                return true;
            }
            await sleep(200);
        }

        return !isProcessAlive(pid);
    }

    try {
        const chromePids = findChromePidFiles(snapDir);
        const hookPids = findChromeHookPidFiles(snapDir);
        const handledHookPids = new Set();

        for (const {pidFile, chromeDir, sessionDir} of chromePids) {
            const resolvedCrawlDir = findOwningCrawlDir(sessionDir);

            if (excludeCrawlDirs.has(resolvedCrawlDir)) {
                continue;
            }
            if (excludeSessionDirs.has(resolvedCrawlDir)) {
                continue;
            }
            if (crawlHeartbeatIsAlive(resolvedCrawlDir)) {
                continue;
            }

            // Crawl is stale, check PID
            try {
                const pid = parseInt(fs.readFileSync(pidFile, 'utf8').trim(), 10);
                if (isNaN(pid) || pid <= 0) continue;

                // Check if process exists
                try {
                    process.kill(pid, 0);
                } catch (e) {
                    // Process dead, remove stale PID file
                    try { fs.unlinkSync(pidFile); } catch (e) {}
                    continue;
                }

                // Process alive and crawl is stale - zombie!
                if (!quiet) console.error(`[!] Found zombie (PID ${pid}) from stale crawl ${path.basename(resolvedCrawlDir)}`);

                try {
                    if (await killChrome(pid, chromeDir)) {
                        killed++;
                        if (!quiet) console.error(`[+] Killed zombie (PID ${pid})`);
                    } else if (!quiet) {
                        console.error(`[!] Failed to fully kill zombie (PID ${pid})`);
                    }
                    try { fs.unlinkSync(pidFile); } catch (e) {}
                } catch (e) {
                    if (!quiet) console.error(`[!] Failed to kill PID ${pid}: ${e.message}`);
                }
            } catch (e) {
                // Skip invalid PID files
            }
        }

        for (const {pidFile, hookName, sessionDir} of hookPids) {
            const resolvedCrawlDir = findOwningCrawlDir(sessionDir);

            try {
                const pid = parseInt(fs.readFileSync(pidFile, 'utf8').trim(), 10);
                if (isNaN(pid) || pid <= 0) continue;
                if (pid === currentPid) continue;
                if (!isProcessAlive(pid)) {
                    try { fs.unlinkSync(pidFile); } catch (error) {}
                    continue;
                }
                handledHookPids.add(pid);
                if (crawlHeartbeatIsAlive(resolvedCrawlDir)) {
                    continue;
                }

                if (!quiet) {
                    console.error(`[!] Found stale chrome hook ${hookName} (PID ${pid}) from crawl ${path.basename(resolvedCrawlDir)}`);
                }
                if (await killHookProcess(pid, hookName)) {
                    killed++;
                    if (!quiet) {
                        console.error(`[+] Killed stale chrome hook ${hookName} (PID ${pid})`);
                    }
                    try { fs.unlinkSync(pidFile); } catch (error) {}
                } else if (!quiet) {
                    console.error(`[!] Failed to kill stale chrome hook ${hookName} (PID ${pid})`);
                }
            } catch (error) {
                // Skip invalid PID files
            }
        }

        for (const {pid, hookName} of findChromeHookProcesses()) {
            if (handledHookPids.has(pid)) {
                continue;
            }
            if (pid === currentPid) {
                continue;
            }
            const currentWorkingDir = getProcessWorkingDir(pid);
            if (!currentWorkingDir) {
                continue;
            }
            const sessionDir = path.basename(currentWorkingDir) === 'chrome'
                ? path.dirname(currentWorkingDir)
                : currentWorkingDir;
            const resolvedCrawlDir = findOwningCrawlDir(sessionDir);
            if (crawlHeartbeatIsAlive(resolvedCrawlDir)) {
                continue;
            }
            if (!quiet) {
                console.error(`[!] Found orphaned chrome hook ${hookName} (PID ${pid}) from crawl ${path.basename(resolvedCrawlDir)}`);
            }
            if (await killHookProcess(pid, hookName)) {
                killed++;
                if (!quiet) {
                    console.error(`[+] Killed orphaned chrome hook ${hookName} (PID ${pid})`);
                }
            } else if (!quiet) {
                console.error(`[!] Failed to kill orphaned chrome hook ${hookName} (PID ${pid})`);
            }
        }
    } catch (e) {
        if (!quiet) console.error(`[!] Error scanning for Chrome processes: ${e.message}`);
    }

    if (killed > 0) {
        if (!quiet) console.error(`[+] Killed ${killed} zombie process(es)`);
    } else {
        if (!quiet) console.error('[+] No zombies found');
    }

    // Clean up stale SingletonLock files from persona chrome_user_data directories
    const personasDir = getPersonasDir();
    if (fs.existsSync(personasDir)) {
        try {
            const personas = fs.readdirSync(personasDir, { withFileTypes: true });
            for (const persona of personas) {
                if (!persona.isDirectory()) continue;

                const userDataDir = path.join(personasDir, persona.name, 'chrome_user_data');
                const singletonLock = path.join(userDataDir, 'SingletonLock');

                if (fs.existsSync(singletonLock)) {
                    try {
                        fs.unlinkSync(singletonLock);
                        if (!quiet) console.error(`[+] Removed stale SingletonLock: ${singletonLock}`);
                    } catch (e) {
                        // Ignore - may be in use by active Chrome
                    }
                }
            }
        } catch (e) {
            // Ignore errors scanning personas directory
        }
    }

    return killed;
}

// ============================================================================
// Chrome launching
// ============================================================================

/**
 * Launch Chromium and return the live browser process + browser-level CDP endpoint.
 *
 * This helper only performs process startup and debug-port verification. It is
 * intentionally earlier in the lifecycle than the crawl launch hook's
 * "published readiness" step:
 * - it may write `chrome.pid` immediately for later cleanup/re-attachment
 * - it does NOT publish `cdp_url.txt` as a stable crawl marker
 * - callers must finish any runtime setup that should happen before other
 *   hooks attach (downloads via CDP, cookie seeding, extension discovery, etc.)
 *   and only then write the crawl/session readiness files
 *
 * Snapshot hooks should therefore wait on the persisted session markers emitted
 * by the crawl launch hook, not on this raw launch result alone.
 *
 * @param {Object} options - Launch options
 * @param {string} [options.binary] - Chrome binary path (auto-detected if not provided)
 * @param {string} [options.outputDir='chrome'] - Directory for output files
 * @param {string} [options.userDataDir] - Chrome user data directory for persistent sessions
 * @param {string} [options.resolution='1440,2000'] - Window resolution
 * @param {boolean} [options.headless=true] - Run in headless mode
 * @param {boolean} [options.sandbox=true] - Enable Chrome sandbox
 * @param {boolean} [options.checkSsl=true] - Check SSL certificates
 * @param {string[]} [options.extensionPaths=[]] - Paths to unpacked extensions
 * @returns {Promise<Object>} - {success, cdpUrl, pid, port, process, error}
 */
async function launchChromium(options = {}) {
    const {
        binary = findChromium(),
        outputDir = 'chrome',
        userDataDir = getEnv('CHROME_USER_DATA_DIR'),
        resolution = getEnv('CHROME_RESOLUTION') || getEnv('RESOLUTION', '1440,2000'),
        userAgent = getEnv('CHROME_USER_AGENT') || getEnv('USER_AGENT', ''),
        headless = getEnvBool('CHROME_HEADLESS', true),
        sandbox = getEnvBool('CHROME_SANDBOX', true),
        checkSsl = getEnvBool('CHROME_CHECK_SSL_VALIDITY', getEnvBool('CHECK_SSL_VALIDITY', true)),
        extensionPaths = [],
    } = options;
    const config = loadConfig(path.join(__dirname, 'config.json'));
    const maxLaunchAttempts = Math.max(1, Number(config.CHROME_LAUNCH_ATTEMPTS) || 1);

    if (!binary) {
        return { success: false, error: 'Chrome binary not found' };
    }

    const { width, height } = parseResolution(resolution);

    // Create output directory
    if (!fs.existsSync(outputDir)) {
        fs.mkdirSync(outputDir, { recursive: true });
    }

    // Create user data directory if specified and doesn't exist
    if (userDataDir) {
        if (!fs.existsSync(userDataDir)) {
            fs.mkdirSync(userDataDir, { recursive: true });
            console.error(`[*] Created user data directory: ${userDataDir}`);
        }
        // Clean up any stale SingletonLock file from previous crashed sessions
        const singletonLock = path.join(userDataDir, 'SingletonLock');
        if (fs.existsSync(singletonLock)) {
            try {
                fs.unlinkSync(singletonLock);
                console.error(`[*] Removed stale SingletonLock: ${singletonLock}`);
            } catch (e) {
                console.error(`[!] Failed to remove SingletonLock: ${e.message}`);
            }
        }
    }

    // Find a free port
    const debugPort = await findFreePort();
    console.error(`[*] Using debug port: ${debugPort}`);

    // Get base Chrome args from config (static flags from CHROME_ARGS env var)
    // These come from config.json defaults, merged by get_config() in Python
    const baseArgs = getEnvArray('CHROME_ARGS', []);

    // Get extra user-provided args
    const extraArgs = getEnvArray('CHROME_ARGS_EXTRA', []);

    // Build dynamic Chrome arguments (these must be computed at runtime)
    const dynamicArgs = [
        // Remote debugging setup
        `--remote-debugging-port=${debugPort}`,
        '--remote-debugging-address=127.0.0.1',

        // Sandbox settings
        ...(sandbox ? [] : ['--no-sandbox', '--disable-setuid-sandbox']),

        // Docker-specific workarounds
        '--disable-dev-shm-usage',

        // Window size
        `--window-size=${width},${height}`,

        // User data directory (for persistent sessions with persona)
        ...(userDataDir ? [`--user-data-dir=${userDataDir}`] : []),

        // User agent
        ...(userAgent ? [`--user-agent=${userAgent}`] : []),

        // Headless mode
        ...(headless ? ['--headless=new'] : []),

        // SSL certificate checking
        ...(checkSsl ? [] : ['--ignore-certificate-errors']),
    ];

    // Combine all args: base (from config) + dynamic (runtime) + extra (user overrides)
    // Dynamic args come after base so they can override if needed
    const chromiumArgs = [...baseArgs, ...dynamicArgs, ...extraArgs];

    // Ensure keychain prompts are disabled on macOS
    if (!chromiumArgs.includes('--use-mock-keychain')) {
        chromiumArgs.push('--use-mock-keychain');
    }

    // Add extension loading flags
    if (extensionPaths.length > 0) {
        const extPathsArg = extensionPaths.join(',');
        chromiumArgs.push(`--load-extension=${extPathsArg}`);
        chromiumArgs.push('--enable-unsafe-extension-debugging');
        chromiumArgs.push('--disable-features=DisableLoadExtensionCommandLineSwitch,ExtensionManifestV2Unsupported,ExtensionManifestV2Disabled');
        console.error(`[*] Loading ${extensionPaths.length} extension(s) via --load-extension`);
    }

    chromiumArgs.push('about:blank');

    // Write command script for debugging
    writeCmdScript(path.join(outputDir, 'cmd.sh'), binary, chromiumArgs);

    const chromeLaunchLock = path.join(getPersonasDir(), '.chrome-launch.lock');
    let lastError = 'Unknown Chromium launch failure';

    // Chromium startup has two distinct phases:
    // 1. process/bootstrap: the native browser process starts, initializes the
    //    profile, binds the remote debugging port, and prints DevTools metadata
    // 2. post-port stabilization: the browser remains alive long enough for a
    //    real CDP client to attach and for the initial about:blank page to be
    //    usable
    //
    // In principle this should be deterministic, but in practice we sometimes
    // see first-launch native failures inside Chromium itself, especially when
    // using a fresh profile and/or loading unpacked extensions in headless mode.
    // Those crashes happen *after* we have already done our deterministic setup
    // (profile dir creation, SingletonLock cleanup, debug port selection, args
    // construction, launch locking), so there is no higher-level app signal we
    // can check in advance to know the first attempt will die.
    //
    // The important boundary here is that we only retry failures that clearly
    // occurred during Chromium's own early startup lifecycle:
    // - the process exits before the DevTools port is ready
    // - the process exits during the short post-launch settle window
    // - the DevTools socket opens, but a real CDP session cannot be stabilized
    //
    // We intentionally do *not* retry arbitrary failures forever. Persistent
    // config issues (bad binary path, invalid flags, broken permissions, etc.)
    // should still fail deterministically on the first attempt.
    for (let attempt = 1; attempt <= maxLaunchAttempts; attempt++) {
        let chromiumProcess = null;
        let chromePid = null;
        let recentStderr = '';
        let recentStdout = '';
        let releaseLaunchLock = null;

        try {
            releaseLaunchLock = await acquireSessionLock(
                chromeLaunchLock,
                getEnvInt('CHROME_LAUNCH_LOCK_TIMEOUT_MS', 120000)
            );
            console.error(`[*] Spawning Chromium (headless=${headless}) [attempt ${attempt}/${maxLaunchAttempts}]...`);
            chromiumProcess = spawn(binary, chromiumArgs, {
                stdio: ['ignore', 'pipe', 'pipe'],
                detached: true,
            });

            chromePid = chromiumProcess.pid;
            const chromeStartTime = Date.now() / 1000;

            if (chromePid) {
                console.error(`[*] Chromium spawned (PID: ${chromePid})`);
                writePidWithMtime(path.join(outputDir, 'chrome.pid'), chromePid, chromeStartTime);
            }

            chromiumProcess.stdout.on('data', (data) => {
                recentStdout = `${recentStdout}${String(data)}`.slice(-4000);
                process.stderr.write(`[chromium:stdout] ${data}`);
            });
            chromiumProcess.stderr.on('data', (data) => {
                recentStderr = `${recentStderr}${String(data)}`.slice(-4000);
                process.stderr.write(`[chromium:stderr] ${data}`);
            });

            // This watches the raw spawned process before we have a reliable CDP
            // session. If Chromium crashes here, all we know is the native exit
            // code/signal and a small stderr tail.
            const chromiumExit = new Promise((_, reject) => {
                chromiumProcess.once('error', (error) => {
                    reject(new Error(`Chromium process failed to start: ${error.message}`));
                });
                chromiumProcess.once('exit', (code, signal) => {
                    reject(new Error(
                        `Chromium exited before opening the debug port (code=${code ?? 'null'}, signal=${signal || 'none'})`
                    ));
                });
            });
            chromiumExit.catch(() => {});

            // The DevTools port coming up is only a coarse readiness signal.
            // Chromium can still crash immediately afterwards, so we follow this
            // with verifyStableChromiumSession() before declaring success.
            console.error(`[*] Waiting for debug port ${debugPort}...`);
            const debugProbeTimeoutMs = getEnvInt('CHROME_DEBUG_PORT_TIMEOUT_MS', 30000);
            const versionInfo = await Promise.race([
                waitForDebugPort(debugPort, debugProbeTimeoutMs),
                chromiumExit,
            ]);
            const wsUrl = versionInfo.webSocketDebuggerUrl;

            console.error(`[+] Chromium ready: ${wsUrl}`);

            const result = {
                success: true,
                cdpUrl: wsUrl,
                pid: chromePid,
                port: debugPort,
                process: chromiumProcess,
            };

            await verifyStableChromiumSession({
                chromePid,
                cdpUrl: wsUrl,
                outputDir,
                headless,
                extensionPaths,
            });

            return result;
        } catch (e) {
            if (chromePid) {
                await cleanupLaunchArtifacts(outputDir, chromePid);
            }
            const extraOutput = [
                recentStdout ? `stdout=${recentStdout.trim()}` : '',
                recentStderr ? `stderr=${recentStderr.trim()}` : '',
            ].filter(Boolean).join(' ');
            lastError = extraOutput
                ? `${e.name}: ${e.message} (${extraOutput})`
                : `${e.name}: ${e.message}`;
            // Only retry failures that map to Chromium's startup/stabilization
            // window. Everything else should bubble out directly so permanent
            // misconfiguration still fails fast and loudly.
            const isTransientStartupFailure =
                lastError.includes('Chromium exited before opening the debug port') ||
                lastError.includes('Timeout waiting for Chrome debug port') ||
                lastError.includes('Chromium exited during startup') ||
                lastError.includes('Chromium exited after opening the debug port') ||
                lastError.includes('Chromium CDP session not stable after startup');
            if (attempt >= maxLaunchAttempts || !isTransientStartupFailure) {
                return {
                    success: false,
                    error: lastError,
                };
            }
            console.error(`[!] Chromium launch attempt ${attempt}/${maxLaunchAttempts} failed, retrying...`);
            await sleep(1000);
        } finally {
            if (releaseLaunchLock) {
                releaseLaunchLock();
            }
        }
    }

    return { success: false, error: lastError };
}

/**
 * Check if a process is still running.
 * @param {number} pid - Process ID to check
 * @returns {boolean} - True if process exists
 */
function isProcessAlive(pid) {
    try {
        process.kill(pid, 0);  // Signal 0 checks existence without killing
        return true;
    } catch (e) {
        return false;
    }
}

async function acquireSessionLock(lockFile, timeoutMs = 10000, intervalMs = 100) {
    const startedAt = Date.now();
    const token = `${process.pid}:${startedAt}:${Math.random().toString(16).slice(2)}`;
    const staleLockMs = Math.max(2000, intervalMs * 10);

    while (Date.now() - startedAt < timeoutMs) {
        try {
            const fd = fs.openSync(lockFile, 'wx');
            fs.writeFileSync(fd, JSON.stringify({ pid: process.pid, token, createdAt: new Date().toISOString() }));
            fs.closeSync(fd);
            return () => {
                try {
                    const current = JSON.parse(fs.readFileSync(lockFile, 'utf-8'));
                    if (current?.token === token) {
                        fs.unlinkSync(lockFile);
                    }
                } catch (error) {}
            };
        } catch (error) {
            if (error?.code !== 'EEXIST') throw error;
            try {
                const current = JSON.parse(fs.readFileSync(lockFile, 'utf-8'));
                if (!current?.pid || !isProcessAlive(current.pid)) {
                    fs.unlinkSync(lockFile);
                    continue;
                }
            } catch (readError) {
                try {
                    const stat = fs.statSync(lockFile);
                    const ageMs = Date.now() - stat.mtimeMs;
                    if (ageMs >= staleLockMs) {
                        fs.unlinkSync(lockFile);
                        continue;
                    }
                } catch (statError) {}
            }
        }
        await sleep(intervalMs);
    }

    throw new Error(`Timeout acquiring lock: ${path.basename(lockFile)}`);
}

/**
 * Find all Chrome child processes for a given debug port.
 * @param {number} port - Debug port number
 * @returns {Array<number>} - Array of PIDs
 */
function findChromeProcessesByPort(port) {
    const { execSync } = require('child_process');
    const pids = [];

    try {
        // Find all Chrome processes using this debug port
        const output = execSync(
            `ps aux | grep -i "chrome.*--remote-debugging-port=${port}" | grep -v grep | awk '{print $2}'`,
            { encoding: 'utf8', timeout: 5000 }
        );

        for (const line of output.split('\n')) {
            const pid = parseInt(line.trim(), 10);
            if (!isNaN(pid) && pid > 0) {
                pids.push(pid);
            }
        }
    } catch (e) {
        // Command failed or no processes found
    }

    return pids;
}

async function waitForChromeProcessTreeExit(pid, debugPort = null, timeoutMs = 5000, intervalMs = 200) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
        const mainAlive = pid ? isProcessAlive(pid) : false;
        const relatedPids = debugPort ? findChromeProcessesByPort(debugPort) : [];
        if (!mainAlive && relatedPids.length === 0) {
            return true;
        }
        await sleep(intervalMs);
    }

    const mainAlive = pid ? isProcessAlive(pid) : false;
    const relatedPids = debugPort ? findChromeProcessesByPort(debugPort) : [];
    return !mainAlive && relatedPids.length === 0;
}

/**
 * Kill a Chrome process by PID.
 * Always sends SIGTERM before SIGKILL, then verifies death.
 *
 * @param {number} pid - Process ID to kill
 * @param {string} [outputDir] - Directory containing PID files to clean up
 */
async function killChrome(pid, outputDir = null) {
    // Get debug port for finding child processes
    let debugPort = null;
    if (outputDir) {
        try {
            const cdpFile = path.join(outputDir, 'cdp_url.txt');
            if (fs.existsSync(cdpFile)) {
                debugPort = getChromeDebugPortFromCdpUrl(fs.readFileSync(cdpFile, 'utf8').trim());
            }
        } catch (e) {}
    }

    const initialRelatedPids = debugPort ? findChromeProcessesByPort(debugPort) : [];
    const hasLiveParent = Boolean(pid && isProcessAlive(pid));
    if (!hasLiveParent && initialRelatedPids.length === 0) {
        return true;
    }

    console.error(
        `[*] Killing Chrome process tree (${hasLiveParent ? `PID ${pid}` : `port ${debugPort}`})...`
    );

    // Step 1: Ask the main browser process to exit cleanly. Chromium itself is
    // responsible for shutting down its renderer/helper children without
    // corrupting the profile dir, so we only send SIGTERM to the parent.
    if (hasLiveParent) {
        console.error(`[*] Sending SIGTERM to Chrome parent process ${pid}...`);
        try {
            process.kill(pid, 'SIGTERM');
        } catch (error) {
            if (error.code !== 'ESRCH') {
                console.error(`[!] SIGTERM failed: ${error.message}`);
            }
        }
    }

    let processTreeExited = await waitForChromeProcessTreeExit(pid, debugPort, 5000);
    if (processTreeExited) {
        console.error('[+] Chrome process tree terminated gracefully');
    } else {
        const remainingPids = new Set();
        if (pid) {
            remainingPids.add(pid);
        }
        for (const relatedPid of debugPort ? findChromeProcessesByPort(debugPort) : initialRelatedPids) {
            remainingPids.add(relatedPid);
        }

        console.error(
            `[*] Chrome did not exit cleanly in time, sending SIGKILL to ${remainingPids.size} remaining processes...`
        );
        for (const remainingPid of remainingPids) {
            if (!remainingPid || !isProcessAlive(remainingPid)) {
                continue;
            }
            try {
                process.kill(remainingPid, 'SIGKILL');
            } catch (error) {
                if (error.code !== 'ESRCH') {
                    console.error(`[!] SIGKILL failed for ${remainingPid}: ${error.message}`);
                }
            }
        }

        processTreeExited = await waitForChromeProcessTreeExit(pid, debugPort, 5000);
        if (!processTreeExited) {
            console.error(`[!] WARNING: Chrome process tree for PID ${pid} is still alive after SIGKILL`);
            console.error(`[!] This typically means Chromium is stuck in an uninterruptible kernel wait state`);
        } else {
            console.error('[+] Chrome process tree killed successfully');
        }
    }

    // Step 8: Clean up PID files
    // Note: hook-specific .pid files are cleaned up by run_hook() and Snapshot.cleanup()
    if (outputDir && processTreeExited) {
        try { fs.unlinkSync(path.join(outputDir, 'chrome.pid')); } catch (e) {}
    }

    if (!processTreeExited) {
        console.error('[!] Chrome cleanup completed, but some browser processes are still alive');
        return false;
    }

    console.error('[*] Chrome cleanup completed');
    return true;
}

/**
 * Install Chromium using @puppeteer/browsers programmatic API.
 * Uses puppeteer's default cache location, returns the binary path.
 *
 * @param {Object} options - Install options
 * @returns {Promise<Object>} - {success, binary, version, error}
 */
async function installChromium(options = {}) {
    // Check if CHROME_BINARY is already set and valid
    const configuredBinary = getEnv('CHROME_BINARY');
    const resolvedConfiguredBinary = resolveBinaryReference(configuredBinary);
    if (resolvedConfiguredBinary) {
        console.error(`[+] Using configured CHROME_BINARY: ${resolvedConfiguredBinary}`);
        return { success: true, binary: resolvedConfiguredBinary, version: null };
    }

    // Try to load @puppeteer/browsers from NODE_MODULES_DIR or system
    let puppeteerBrowsers;
    try {
        ensureNodeModuleResolution(module);
        puppeteerBrowsers = require('@puppeteer/browsers');
    } catch (e) {
        console.error(`[!] @puppeteer/browsers not found. Install it first with installPuppeteerCore.`);
        return { success: false, error: '@puppeteer/browsers not installed' };
    }

    console.error(`[*] Installing Chromium via @puppeteer/browsers...`);

    try {
        const result = await puppeteerBrowsers.install({
            browser: 'chromium',
            buildId: 'latest',
        });

        const binary = result.executablePath;
        const version = result.buildId;

        if (!binary || !fs.existsSync(binary)) {
            console.error(`[!] Chromium binary not found at: ${binary}`);
            return { success: false, error: `Chromium binary not found at: ${binary}` };
        }

        console.error(`[+] Chromium installed: ${binary}`);
        return { success: true, binary, version };
    } catch (e) {
        console.error(`[!] Failed to install Chromium: ${e.message}`);
        return { success: false, error: e.message };
    }
}

/**
 * Install puppeteer-core npm package.
 *
 * @param {Object} options - Install options
 * @param {string} [options.npmPrefix] - npm prefix directory (default: LIB_DIR/npm)
 * @param {number} [options.timeout=60000] - Timeout in milliseconds
 * @returns {Promise<Object>} - {success, path, error}
 */
async function installPuppeteerCore(options = {}) {
    const arch = `${process.arch}-${process.platform}`;
    const defaultPrefix = path.join(getLibDir(), 'npm');
    const {
        npmPrefix = defaultPrefix,
        timeout = 60000,
    } = options;

    const nodeModulesDir = path.join(npmPrefix, 'node_modules');
    const puppeteerPath = path.join(nodeModulesDir, 'puppeteer-core');

    // Check if already installed
    if (fs.existsSync(puppeteerPath)) {
        console.error(`[+] puppeteer-core already installed: ${puppeteerPath}`);
        return { success: true, path: puppeteerPath };
    }

    console.error(`[*] Installing puppeteer-core to ${npmPrefix}...`);

    // Create directory
    if (!fs.existsSync(npmPrefix)) {
        fs.mkdirSync(npmPrefix, { recursive: true });
    }

    try {
        const { execSync } = require('child_process');
        execSync(
            `npm install --prefix "${npmPrefix}" puppeteer-core`,
            { encoding: 'utf8', timeout, stdio: ['pipe', 'pipe', 'pipe'] }
        );
        console.error(`[+] puppeteer-core installed successfully`);
        return { success: true, path: puppeteerPath };
    } catch (e) {
        console.error(`[!] Failed to install puppeteer-core: ${e.message}`);
        return { success: false, error: e.message };
    }
}

// Try to import unzipper, fallback to system unzip if not available
let unzip = null;
try {
    const unzipper = require('unzipper');
    unzip = async (sourcePath, destPath) => {
        const stream = fs.createReadStream(sourcePath).pipe(unzipper.Extract({ path: destPath }));
        return stream.promise();
    };
} catch (err) {
    // Will use system unzip command as fallback
}

/**
 * Compute the extension ID from the unpacked path.
 * Chrome uses a SHA256 hash of the unpacked extension directory path to compute a dynamic id.
 *
 * @param {string} unpacked_path - Path to the unpacked extension directory
 * @returns {string} - 32-character extension ID
 */
function getExtensionId(unpacked_path) {
    let resolved_path = unpacked_path;
    try {
        resolved_path = fs.realpathSync(unpacked_path);
    } catch (err) {
        // Use the provided path if realpath fails
        resolved_path = unpacked_path;
    }
    // Chrome uses a SHA256 hash of the unpacked extension directory path
    const hash = crypto.createHash('sha256');
    hash.update(Buffer.from(resolved_path, 'utf-8'));

    // Convert first 32 hex chars to characters in the range 'a'-'p'
    const detected_extension_id = Array.from(hash.digest('hex'))
        .slice(0, 32)
        .map(i => String.fromCharCode(parseInt(i, 16) + 'a'.charCodeAt(0)))
        .join('');

    return detected_extension_id;
}

/**
 * Download and install a Chrome extension from the Chrome Web Store.
 *
 * @param {Object} extension - Extension metadata object
 * @param {string} extension.webstore_id - Chrome Web Store extension ID
 * @param {string} extension.name - Human-readable extension name
 * @param {string} extension.crx_url - URL to download the CRX file
 * @param {string} extension.crx_path - Local path to save the CRX file
 * @param {string} extension.unpacked_path - Path to extract the extension
 * @returns {Promise<boolean>} - True if installation succeeded
 */
async function installExtension(extension, options = {}) {
    const {
        forceInstall = false,
    } = options;
    const manifest_path = path.join(extension.unpacked_path, 'manifest.json');

    // Re-fetch the CRX when explicitly forcing a refresh; otherwise reuse the
    // existing archive if we've already downloaded it locally.
    if (forceInstall || (!fs.existsSync(manifest_path) && !fs.existsSync(extension.crx_path))) {
        console.log(`[🛠️] Downloading missing extension ${extension.name} ${extension.webstore_id} -> ${extension.crx_path}`);

        try {
            // Ensure parent directory exists
            const crxDir = path.dirname(extension.crx_path);
            if (!fs.existsSync(crxDir)) {
                fs.mkdirSync(crxDir, { recursive: true });
            }

            // Download CRX file from Chrome Web Store
            const response = await fetch(extension.crx_url);

            if (!response.ok) {
                console.warn(`[⚠️] Failed to download extension ${extension.name}: HTTP ${response.status}`);
                return false;
            }

            if (response.body) {
                const crx_file = fs.createWriteStream(extension.crx_path);
                const crx_stream = Readable.fromWeb(response.body);
                await finished(crx_stream.pipe(crx_file));
            } else {
                console.warn(`[⚠️] Failed to download extension ${extension.name}: No response body`);
                return false;
            }
        } catch (err) {
            console.error(`[❌] Failed to download extension ${extension.name}:`, err);
            return false;
        }
    }

    // Unzip CRX file to unpacked_path. CRX files are ZIP archives with
    // an extra header (magic ``Cr24``, version, public key, signature)
    // prefixed to the real ZIP stream. POSIX ``unzip`` is lenient about
    // the header, but Windows has no ``unzip``, and ``tar``/``Expand-
    // Archive`` are strict. Strip the header in-process first so every
    // platform can extract the resulting plain ZIP with whatever tool
    // it already has.
    await fs.promises.mkdir(extension.unpacked_path, { recursive: true });

    // Locate the local-file header magic (``PK\x03\x04``) that starts
    // the real ZIP payload and write that suffix to a sibling ``.zip``.
    let zipPath = extension.crx_path;
    try {
        const raw = await fs.promises.readFile(extension.crx_path);
        const pkIdx = raw.indexOf(Buffer.from([0x50, 0x4b, 0x03, 0x04]));
        if (pkIdx > 0) {
            zipPath = `${extension.crx_path}.zip`;
            await fs.promises.writeFile(zipPath, raw.subarray(pkIdx));
        }
    } catch (stripErr) {
        console.warn(`[⚠️] Failed to strip CRX header from ${extension.crx_path}:`, stripErr.message);
    }

    // Pick a platform-appropriate extractor. ``tar -xf`` is present on
    // Windows 10 1803+ and every mainstream *nix distro and handles
    // plain ZIP transparently; POSIX ``unzip`` stays as the default
    // elsewhere so we don't regress hosts that already work.
    const unzipCmd = process.platform === 'win32'
        ? `tar -xf "${zipPath}" -C "${extension.unpacked_path}"`
        : `/usr/bin/unzip -q -o "${zipPath}" -d "${extension.unpacked_path}"`;
    try {
        await execAsync(unzipCmd);
    } catch (err1) {
        // Extractors may return non-zero even on success due to CRX
        // header warnings, check if the manifest landed anyway.
        if (!fs.existsSync(manifest_path)) {
            if (unzip) {
                // Fallback to unzipper library
                try {
                    await unzip(zipPath, extension.unpacked_path);
                } catch (err2) {
                    console.error(`[❌] Failed to unzip ${extension.crx_path}:`, err2.message);
                    return false;
                }
            } else {
                console.error(`[❌] Failed to unzip ${extension.crx_path}:`, err1.message);
                return false;
            }
        }
    }

    if (!fs.existsSync(manifest_path)) {
        console.error(`[❌] Failed to install ${extension.crx_path}: could not find manifest.json in unpacked_path`);
        return false;
    }

    return true;
}

/**
 * Load or install a Chrome extension, computing all metadata.
 *
 * @param {Object} ext - Partial extension metadata (at minimum: webstore_id or unpacked_path)
 * @param {string} [ext.webstore_id] - Chrome Web Store extension ID
 * @param {string} [ext.name] - Human-readable extension name
 * @param {string} [ext.unpacked_path] - Path to unpacked extension
 * @param {string} [extensions_dir] - Directory to store extensions
 * @returns {Promise<Object>} - Complete extension metadata object
 */
async function loadOrInstallExtension(ext, extensions_dir = null, force_install = false) {
    if (!(ext.webstore_id || ext.unpacked_path)) {
        throw new Error('Extension must have either {webstore_id} or {unpacked_path}');
    }

    // Determine extensions directory
    // Use provided dir, or fall back to getExtensionsDir() which handles env vars and defaults
    const EXTENSIONS_DIR = extensions_dir || getExtensionsDir();

    // Set statically computable extension metadata
    ext.webstore_id = ext.webstore_id || ext.id;
    ext.name = ext.name || ext.webstore_id;
    ext.webstore_url = ext.webstore_url || `https://chromewebstore.google.com/detail/${ext.webstore_id}`;
    ext.crx_url = ext.crx_url || `https://clients2.google.com/service/update2/crx?response=redirect&prodversion=1230&acceptformat=crx3&x=id%3D${ext.webstore_id}%26uc`;
    ext.crx_path = ext.crx_path || path.join(EXTENSIONS_DIR, `${ext.webstore_id}__${ext.name}.crx`);
    ext.unpacked_path = ext.unpacked_path || path.join(EXTENSIONS_DIR, `${ext.webstore_id}__${ext.name}`);

    const manifest_path = path.join(ext.unpacked_path, 'manifest.json');
    ext.read_manifest = () => JSON.parse(fs.readFileSync(manifest_path, 'utf-8'));
    ext.read_version = () => fs.existsSync(manifest_path) && ext.read_manifest()?.version || null;

    // If extension is not installed, download and unpack it
    if (force_install || !ext.read_version()) {
        await installExtension(ext, { forceInstall: force_install });
    }

    // Autodetect ID from filesystem path (unpacked extensions don't have stable IDs)
    ext.id = getExtensionId(ext.unpacked_path);
    ext.version = ext.read_version();

    if (!ext.version) {
        console.warn(`[❌] Unable to detect ID and version of installed extension ${ext.unpacked_path}`);
    } else {
        console.log(`[➕] Installed extension ${ext.name} (${ext.version})... ${ext.unpacked_path}`);
    }

    return ext;
}

/**
 * Check if a Puppeteer target is an extension background page/service worker.
 *
 * @param {Object} target - Puppeteer target object
 * @returns {Promise<Object>} - Object with target_is_bg, extension_id, manifest_version, etc.
 */
const CHROME_EXTENSION_URL_PREFIX = 'chrome-extension://';
const EXTENSION_BACKGROUND_TARGET_TYPES = new Set(['service_worker', 'background_page']);

/**
 * Parse extension ID from a target URL.
 *
 * @param {string|null|undefined} targetUrl - URL from Puppeteer target
 * @returns {string|null} - Extension ID if URL is a chrome-extension URL
 */
function getExtensionIdFromUrl(targetUrl) {
    if (!targetUrl || !targetUrl.startsWith(CHROME_EXTENSION_URL_PREFIX)) return null;
    return targetUrl.slice(CHROME_EXTENSION_URL_PREFIX.length).split('/')[0] || null;
}

/**
 * Filter extension list to entries with unpacked paths.
 *
 * @param {Array} extensions - Extension metadata list
 * @returns {Array} - Extensions with unpacked_path
 */
function getValidInstalledExtensions(extensions) {
    if (!Array.isArray(extensions) || extensions.length === 0) return [];
    return extensions.filter(ext => ext?.unpacked_path);
}

async function tryGetExtensionContext(target, targetType) {
    if (targetType === 'service_worker') return await target.worker();
    return await target.page();
}

async function waitForExtensionTargetType(browser, extensionId, targetType, timeout) {
    const target = await browser.waitForTarget(
        candidate => candidate.type() === targetType &&
            getExtensionIdFromUrl(candidate.url()) === extensionId,
        { timeout }
    );
    return await tryGetExtensionContext(target, targetType);
}

/**
 * Wait for a Puppeteer target handle for a specific extension id.
 *
 * @param {Object} browser - Puppeteer browser instance
 * @param {string} extensionId - Extension ID
 * @param {number} [timeout=30000] - Timeout in milliseconds
 * @param {string|null} [preferredTargetUrl=null] - Exact extension target URL to prefer
 * @returns {Promise<Object>} - Puppeteer target
 */
async function waitForExtensionTargetHandle(browser, extensionId, timeout = 30000, preferredTargetUrl = null) {
    const deadline = Date.now() + Math.max(timeout, 0);
    let lastCandidates = [];

    while (Date.now() < deadline) {
        const candidates = browser.targets().filter(target =>
            getExtensionIdFromUrl(target.url()) === extensionId &&
            (EXTENSION_BACKGROUND_TARGET_TYPES.has(target.type()) ||
                target.url().startsWith(CHROME_EXTENSION_URL_PREFIX))
        );

        if (preferredTargetUrl) {
            const exactMatch = candidates.find(target => target.url() === preferredTargetUrl);
            if (exactMatch) {
                return exactMatch;
            }
        } else {
            const backgroundTarget = candidates.find(target => EXTENSION_BACKGROUND_TARGET_TYPES.has(target.type()));
            if (backgroundTarget) {
                return backgroundTarget;
            }
            if (candidates.length > 0) {
                return candidates[0];
            }
        }

        lastCandidates = candidates.map(target => `${target.type()}:${target.url()}`);
        await sleep(100);
    }

    const error = new Error(
        `Timed out waiting for extension target ${extensionId}` +
        (preferredTargetUrl ? ` (${preferredTargetUrl})` : '') +
        (lastCandidates.length ? `; last seen: ${lastCandidates.join(', ')}` : '')
    );
    error.name = 'TimeoutError';
    throw error;
}

async function isTargetExtension(target) {
    let target_type;
    let target_ctx;
    let target_url;

    try {
        target_type = target.type();
        target_ctx = (await target.worker()) || (await target.page()) || null;
        target_url = target.url() || target_ctx?.url() || null;
    } catch (err) {
        if (String(err).includes('No target with given id found')) {
            // Target closed during check, ignore harmless race condition
            target_type = 'closed';
            target_ctx = null;
            target_url = 'about:closed';
        } else {
            throw err;
        }
    }

    // Check if this is an extension background page or service worker
    const extension_id = getExtensionIdFromUrl(target_url);
    const is_chrome_extension = Boolean(extension_id);
    const is_background_page = target_type === 'background_page';
    const is_service_worker = target_type === 'service_worker';
    const target_is_bg = is_chrome_extension && (is_background_page || is_service_worker);

    let manifest_version = null;
    let manifest = null;
    let manifest_name = null;
    const target_is_extension = is_chrome_extension || target_is_bg;

    if (target_is_extension) {
        try {
            if (target_ctx) {
                manifest = await target_ctx.evaluate(() => chrome.runtime.getManifest());
                manifest_version = manifest?.manifest_version || null;
                manifest_name = manifest?.name || null;
            }
        } catch (err) {
            // Failed to get extension metadata
        }
    }

    return {
        target_is_extension,
        target_is_bg,
        target_type,
        target_ctx,
        target_url,
        extension_id,
        manifest_version,
        manifest,
        manifest_name,
    };
}

/**
 * Load extension metadata and connection handlers from a browser target.
 *
 * @param {Array} extensions - Array of extension metadata objects to update
 * @param {Object} target - Puppeteer target object
 * @returns {Promise<Object|null>} - Updated extension object or null if not an extension
 */
async function loadExtensionFromTarget(extensions, target) {
    const {
        target_is_bg,
        target_is_extension,
        target_type,
        target_ctx,
        target_url,
        extension_id,
        manifest_version,
        manifest,
    } = await isTargetExtension(target);

    if (!(target_is_bg && extension_id && target_ctx)) {
        return null;
    }

    // Find matching extension in our list
    const extension = extensions.find(ext => ext.id === extension_id);
    if (!extension) {
        console.warn(`[⚠️] Found loaded extension ${extension_id} that's not in CHROME_EXTENSIONS list`);
        return null;
    }

    if (!manifest) {
        console.error(`[❌] Failed to read manifest for extension ${extension_id}`);
        return null;
    }

    // Create dispatch methods for communicating with the extension
    const new_extension = {
        ...extension,
        target,
        target_type,
        target_url,
        manifest,
        manifest_version,

        // Trigger extension toolbar button click
        dispatchAction: async (tab) => {
            return await target_ctx.evaluate(async (tab) => {
                const browserApi = (typeof browser !== 'undefined' && browser) || null;
                const chromeApi = (typeof chrome !== 'undefined' && chrome) || null;
                const tabsApi = browserApi?.tabs || chromeApi?.tabs || null;

                if (!tab && tabsApi?.query) {
                    const tabs = await tabsApi.query({ currentWindow: true, active: true });
                    tab = tabs?.[0] || null;
                }

                if (browserApi?.action?.onClicked?.dispatch) {
                    return await browserApi.action.onClicked.dispatch(tab);
                }

                if (chromeApi?.action?.onClicked?.dispatch) {
                    return await chromeApi.action.onClicked.dispatch(tab);
                }

                if (browserApi?.browserAction?.onClicked?.dispatch) {
                    return await browserApi.browserAction.onClicked.dispatch(tab);
                }

                if (chromeApi?.browserAction?.onClicked?.dispatch) {
                    return await chromeApi.browserAction.onClicked.dispatch(tab);
                }

                throw new Error('Extension action dispatch not available');
            }, tab || null);
        },

        // Send message to extension
        dispatchMessage: async (message, options = {}) => {
            return await target_ctx.evaluate((msg, opts) => {
                return new Promise((resolve) => {
                    chrome.runtime.sendMessage(msg, opts, (response) => {
                        resolve(response);
                    });
                });
            }, message, options);
        },

        // Trigger extension command (keyboard shortcut)
        dispatchCommand: async (command) => {
            return await target_ctx.evaluate((cmd) => {
                return new Promise((resolve) => {
                    chrome.commands.onCommand.addListener((receivedCommand) => {
                        if (receivedCommand === cmd) {
                            resolve({ success: true, command: receivedCommand });
                        }
                    });
                    // Note: Actually triggering commands programmatically is not directly supported
                    // This would need to be done via CDP or keyboard simulation
                });
            }, command);
        },
    };

    // Update the extension in the array
    Object.assign(extension, new_extension);

    console.log(`[🔌] Connected to extension ${extension.name} (${extension.version})`);

    return new_extension;
}

/**
 * Install all extensions in the list if not already installed.
 *
 * @param {Array} extensions - Array of extension metadata objects
 * @param {string} [extensions_dir] - Directory to store extensions
 * @returns {Promise<Array>} - Array of installed extension objects
 */
async function installAllExtensions(extensions, extensions_dir = null) {
    console.log(`[⚙️] Installing ${extensions.length} chrome extensions...`);

    for (const extension of extensions) {
        await loadOrInstallExtension(extension, extensions_dir);
    }

    return extensions;
}

/**
 * Load and connect to all extensions from a running browser.
 *
 * @param {Object} browser - Puppeteer browser instance
 * @param {Array} extensions - Array of extension metadata objects
 * @returns {Promise<Array>} - Array of loaded extension objects with connection handlers
 */
async function loadAllExtensionsFromBrowser(browser, extensions, timeout = 30000) {
    console.log(`[⚙️] Loading ${extensions.length} chrome extensions from browser...`);
    const perExtensionTimeout = Math.max(
        250,
        getEnvInt('CHROME_EXTENSION_DISCOVERY_TIMEOUT_MS', Math.min(timeout, 5000))
    );

    for (const extension of getValidInstalledExtensions(extensions)) {
        if (!extension.id) {
            extension.load_error = `Extension ${extension.name || extension.unpacked_path} missing id`;
            console.warn(`[!] ${extension.load_error}, continuing without browser connection`);
            continue;
        }
        try {
            const target = await waitForExtensionTargetHandle(browser, extension.id, perExtensionTimeout);
            await loadExtensionFromTarget(extensions, target);
            delete extension.load_error;
        } catch (error) {
            extension.load_error = `${error.name}: ${error.message}`;
            console.warn(
                `[!] Extension ${extension.name || extension.id} did not expose a background target within ` +
                `${perExtensionTimeout}ms, continuing: ${extension.load_error}`
            );
        }
    }

    return extensions;
}

/**
 * Load extension manifest.json file
 *
 * @param {string} unpacked_path - Path to unpacked extension directory
 * @returns {object|null} - Parsed manifest object or null if not found/invalid
 */
function loadExtensionManifest(unpacked_path) {
    const manifest_path = path.join(unpacked_path, 'manifest.json');

    if (!fs.existsSync(manifest_path)) {
        return null;
    }

    try {
        const manifest_content = fs.readFileSync(manifest_path, 'utf-8');
        return JSON.parse(manifest_content);
    } catch (error) {
        // Invalid JSON or read error
        return null;
    }
}

/**
 * @deprecated Use puppeteer's enableExtensions option instead.
 *
 * Generate Chrome launch arguments for loading extensions.
 * NOTE: This is deprecated. Use puppeteer.launch({ pipe: true, enableExtensions: [paths] }) instead.
 *
 * @param {Array} extensions - Array of extension metadata objects
 * @returns {Array<string>} - Chrome CLI arguments for loading extensions
 */
function getExtensionLaunchArgs(extensions) {
    console.warn('[DEPRECATED] getExtensionLaunchArgs is deprecated. Use puppeteer enableExtensions option instead.');
    const validExtensions = getValidInstalledExtensions(extensions);
    if (validExtensions.length === 0) return [];

    const unpacked_paths = validExtensions.map(ext => ext.unpacked_path);
    // Use computed id (from path hash) for allowlisting, as that's what Chrome uses for unpacked extensions
    // Fall back to webstore_id if computed id not available
    const extension_ids = validExtensions.map(ext => ext.id || getExtensionId(ext.unpacked_path));

    return [
        `--load-extension=${unpacked_paths.join(',')}`,
        `--allowlisted-extension-id=${extension_ids.join(',')}`,
        '--allow-legacy-extension-manifests',
        '--disable-extensions-auto-update',
    ];
}

/**
 * Get extension paths for use with puppeteer's enableExtensions option.
 * Following puppeteer best practices: https://pptr.dev/guides/chrome-extensions
 *
 * @param {Array} extensions - Array of extension metadata objects
 * @returns {Array<string>} - Array of extension unpacked paths
 */
function getExtensionPaths(extensions) {
    return getValidInstalledExtensions(extensions).map(ext => ext.unpacked_path);
}

/**
 * Wait for an extension target to be available in the browser.
 * Following puppeteer best practices for accessing extension contexts.
 *
 * For Manifest V3 extensions (service workers):
 *   const worker = await waitForExtensionTarget(browser, extensionId);
 *   // worker is a WebWorker context
 *
 * For Manifest V2 extensions (background pages):
 *   const page = await waitForExtensionTarget(browser, extensionId);
 *   // page is a Page context
 *
 * @param {Object} browser - Puppeteer browser instance
 * @param {string} extensionId - Extension ID to wait for (computed from path hash)
 * @param {number} [timeout=30000] - Timeout in milliseconds
 * @returns {Promise<Object>} - Worker or Page context for the extension
 */
async function waitForExtensionTarget(browser, extensionId, timeout = 30000) {
    for (const targetType of EXTENSION_BACKGROUND_TARGET_TYPES) {
        try {
            const context = await waitForExtensionTargetType(browser, extensionId, targetType, timeout);
            if (context) return context;
        } catch (err) {
            // Continue to next extension target type
        }
    }

    // Try any extension page as fallback
    const extTarget = await waitForExtensionTargetHandle(browser, extensionId, timeout);

    // Return worker or page depending on target type
    return await tryGetExtensionContext(extTarget, extTarget.type());
}

/**
 * Read extensions metadata from chrome session directory.
 *
 * @param {string} chromeSessionDir - Path to chrome session directory
 * @returns {Array<Object>|null} - Parsed extensions metadata list or null if unavailable
 */
function readExtensionsMetadata(chromeSessionDir) {
    const extensionsFile = path.join(path.resolve(chromeSessionDir), 'extensions.json');
    if (!fs.existsSync(extensionsFile)) return null;
    try {
        const parsed = JSON.parse(fs.readFileSync(extensionsFile, 'utf8'));
        return Array.isArray(parsed) ? parsed : null;
    } catch (e) {
        return null;
    }
}

/**
 * Find extension metadata entry by name.
 *
 * @param {Array<Object>} extensions - Parsed extensions metadata list
 * @param {string} extensionName - Extension name to match
 * @returns {Object|null} - Matching extension metadata entry
 */
function findExtensionMetadataByName(extensions, extensionName) {
    const wanted = (extensionName || '').toLowerCase();
    return extensions.find(ext => (ext?.name || '').toLowerCase() === wanted) || null;
}

/**
 * Get all loaded extension targets from a browser.
 *
 * @param {Object} browser - Puppeteer browser instance
 * @returns {Array<Object>} - Array of extension target info objects
 */
function getExtensionTargets(browser) {
    return browser.targets()
        .filter(target =>
            getExtensionIdFromUrl(target.url()) ||
            EXTENSION_BACKGROUND_TARGET_TYPES.has(target.type())
        )
        .map(target => ({
            type: target.type(),
            url: target.url(),
            extensionId: getExtensionIdFromUrl(target.url()),
        }));
}

/**
 * Resolve the Chromium-family browser binary to launch.
 *
 * Resolution order matters because tests and runtime callers may override the
 * browser at the environment layer:
 * 1. `CHROME_BINARY`, if explicitly provided at runtime
 * 2. installs exposed under `LIB_DIR`
 * 3. Puppeteer cache locations
 * 4. system Chromium locations
 *
 * This helper is intentionally Chromium-oriented. It should not guess at
 * unrelated branded browsers or `/Applications/*` installs when a runtime
 * override or `LIB_DIR` browser is expected to be authoritative.
 *
 * @returns {string|null} - Absolute path to browser binary or null if not found
 */
function findChromium() {
    const { execFileSync } = require('child_process');

    // Helper to validate a binary by running --version
    const validateBinary = (binaryPath) => {
        if (!binaryPath) return false;
        try {
            execFileSync(binaryPath, ['--version'], { encoding: 'utf8', timeout: 5000, stdio: 'pipe' });
            return true;
        } catch (e) {
            return false;
        }
    };

    const resolveBinaryReference = (binaryPath) => {
        if (!binaryPath) return null;

        const hasPathSeparator = binaryPath.includes(path.sep) || (path.sep === '\\' && binaryPath.includes('/'));
        if (path.isAbsolute(binaryPath) || hasPathSeparator) {
            const absPath = path.resolve(binaryPath);
            return validateBinary(absPath) ? absPath : null;
        }

        try {
            const locator = process.platform === 'win32' ? 'where' : 'which';
            const resolved = execFileSync(locator, [binaryPath], {
                encoding: 'utf8',
                timeout: 5000,
                stdio: 'pipe',
            }).split(/\r?\n/).find(Boolean)?.trim();
            return resolved && validateBinary(resolved) ? resolved : null;
        } catch (e) {
            return validateBinary(binaryPath) ? binaryPath : null;
        }
    };

    // 1. Check CHROME_BINARY env var first
    const chromeBinary = getEnv('CHROME_BINARY');
    if (chromeBinary) {
        const resolvedBinary = resolveBinaryReference(chromeBinary);
        if (resolvedBinary) {
            return resolvedBinary;
        }
        console.error(`[!] Warning: CHROME_BINARY="${chromeBinary}" is not valid`);
    }

    // 2. Warn that no CHROME_BINARY is configured, searching fallbacks
    if (!chromeBinary) {
        console.error('[!] Warning: CHROME_BINARY not set, searching system locations...');
    }

    // Helper to find Chromium in @puppeteer/browsers directory structure
    const findInPuppeteerDir = (baseDir) => {
        if (!fs.existsSync(baseDir)) return null;
        try {
            const entryNames = fs.readdirSync(baseDir, { withFileTypes: true })
                .filter(entry => entry.isDirectory())
                .map(entry => entry.name);
            const versionRoots = [baseDir];

            for (const entryName of entryNames) {
                if (entryName === 'chrome' || entryName === 'chromium') {
                    versionRoots.push(path.join(baseDir, entryName));
                }
            }

            for (const versionRoot of versionRoots) {
                if (!fs.existsSync(versionRoot)) continue;
                const versions = fs.readdirSync(versionRoot, { withFileTypes: true })
                    .filter(entry => entry.isDirectory())
                    .map(entry => entry.name)
                    .sort()
                    .reverse();
                for (const version of versions) {
                    const versionDir = path.join(versionRoot, version);
                    const candidates = [
                        path.join(versionDir, 'chrome-mac-arm64/Chromium.app/Contents/MacOS/Chromium'),
                        path.join(versionDir, 'chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing'),
                        path.join(versionDir, 'chrome-mac/Chromium.app/Contents/MacOS/Chromium'),
                        path.join(versionDir, 'chrome-mac/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing'),
                        path.join(versionDir, 'chrome-mac-x64/Chromium.app/Contents/MacOS/Chromium'),
                        path.join(versionDir, 'chrome-mac-x64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing'),
                        path.join(versionDir, 'chrome-linux64/chrome'),
                        path.join(versionDir, 'chrome-linux/chrome'),
                    ];
                    for (const c of candidates) {
                        if (fs.existsSync(c)) return c;
                    }
                }
            }
        } catch (e) {}
        return null;
    };

    // 3. Search LIB_DIR for hook-installed Chromium
    const libDir = getEnv('LIB_DIR');
    if (libDir) {
        const libCandidates = [
            path.join(libDir, 'chrome-linux', 'chrome'),
            path.join(libDir, 'browsers', 'chrome', 'chrome'),
        ];
        for (const c of libCandidates) {
            if (validateBinary(c)) return c;
        }
        // Also search puppeteer cache under LIB_DIR
        const libPuppeteerDirs = [
            path.join(libDir, 'puppeteer', 'chromium'),
            path.join(libDir, 'puppeteer', 'chrome'),
        ];
        for (const libPuppeteerDir of libPuppeteerDirs) {
            const libPuppeteerBinary = findInPuppeteerDir(libPuppeteerDir);
            if (libPuppeteerBinary && validateBinary(libPuppeteerBinary)) {
                return libPuppeteerBinary;
            }
        }
    }

    // 4. Search fallback locations (Chromium only)
    const fallbackLocations = [
        // System Chromium
        '/Applications/Chromium.app/Contents/MacOS/Chromium',
        '/usr/bin/chromium',
        '/usr/bin/chromium-browser',
        // Puppeteer cache
        path.join(process.env.HOME || '', '.cache/puppeteer'),
        path.join(process.env.HOME || '', 'Library/Caches/puppeteer'),
    ];

    for (const loc of fallbackLocations) {
        if (loc.endsWith('/puppeteer') || loc.endsWith('\\puppeteer')) {
            const binary = findInPuppeteerDir(loc);
            if (binary && validateBinary(binary)) {
                return binary;
            }
        } else if (validateBinary(loc)) {
            return loc;
        }
    }

    return null;
}

/**
 * Find Chromium binary path only (never Chrome/Brave/Edge).
 * Prefers CHROME_BINARY if set, then Chromium.
 *
 * @returns {string|null} - Absolute path or command name to browser binary
 */
function findAnyChromiumBinary() {
    const chromiumBinary = findChromium();
    if (chromiumBinary) return chromiumBinary;
    return null;
}

// ============================================================================
// Shared Extension Installer Utilities
// ============================================================================

/**
 * Get the extensions directory path.
 * Centralized path calculation used by extension installers and chrome launch.
 *
 * Path is derived from environment variables in this priority:
 * 1. CHROME_EXTENSIONS_DIR (explicit override)
 * 2. PERSONAS_DIR/ACTIVE_PERSONA/chrome_extensions (default)
 *
 * @returns {string} - Absolute path to extensions directory
 */
function getExtensionsDir() {
    const config = loadConfig(path.join(__dirname, 'config.json'));
    return config.CHROME_EXTENSIONS_DIR ||
        path.join(getPersonasDir(), config.ACTIVE_PERSONA, 'chrome_extensions');
}

/**
 * Get machine type string for platform-specific paths.
 * Matches Python's archivebox.config.paths.get_machine_type()
 *
 * @returns {string} - Machine type (e.g., 'x86_64-linux', 'arm64-darwin')
 */
function getMachineType() {
    if (process.env.MACHINE_TYPE) {
        return process.env.MACHINE_TYPE;
    }

    let machine = process.arch;
    const system = process.platform;

    // Normalize machine type to match Python's convention
    if (machine === 'arm64' || machine === 'aarch64') {
        machine = 'arm64';
    } else if (machine === 'x64' || machine === 'x86_64' || machine === 'amd64') {
        machine = 'x86_64';
    } else if (machine === 'ia32' || machine === 'x86') {
        machine = 'x86';
    }

    return `${machine}-${system}`;
}

/**
 * Get LIB_DIR path for shared binaries and caches.
 * Returns the chrome-hook legacy default (~/.config/abx/lib) if LIB_DIR is
 * unset. This is separate from the main abxpkg CLI's platform-specific
 * default library root handling.
 *
 * @returns {string} - Absolute path to lib directory
 */
function getLibDir() {
    if (process.env.LIB_DIR) {
        return path.resolve(process.env.LIB_DIR);
    }
    const defaultRoot = path.join(os.homedir(), '.config', 'abx', 'lib');
    return path.resolve(defaultRoot);
}

/**
 * Get the canonical `NODE_MODULES_DIR` used to resolve runtime JS deps.
 *
 * Chrome hooks depend on packages such as `puppeteer` and
 * `@puppeteer/browsers`. Python callers should treat this as the source of
 * truth and export the same value through `NODE_PATH` when shelling out to
 * Node so test/runtime resolution stays identical.
 *
 * @returns {string} - Absolute path to node_modules directory
 */
function getNodeModulesDir() {
    return getNodeModulesDirFromBaseUtils();
}

/**
 * Get the shared environment/path contract used by Python tests and JS hooks.
 *
 * This mirrors the runtime path layout closely enough that test helpers can
 * exercise the same launch/session code without re-implementing the path
 * calculation rules. Python should prefer this instead of reconstructing
 * `LIB_DIR`, `NODE_MODULES_DIR`, `CHROME_EXTENSIONS_DIR`, etc. on its own.
 *
 * @returns {Object} - Object with all test environment paths
 */
function getTestEnv() {
    const snapDir = getSnapDir();
    const crawlDir = getCrawlDir();
    const machineType = getMachineType();
    const libDir = getLibDir();
    const nodeModulesDir = getNodeModulesDir();

    return {
        SNAP_DIR: snapDir,
        CRAWL_DIR: crawlDir,
        PERSONAS_DIR: getPersonasDir(),
        ACTIVE_PERSONA: loadConfig(path.join(__dirname, 'config.json')).ACTIVE_PERSONA,
        MACHINE_TYPE: machineType,
        LIB_DIR: libDir,
        NODE_MODULES_DIR: nodeModulesDir,
        NODE_PATH: nodeModulesDir,  // Node.js uses NODE_PATH for module resolution
        NPM_BIN_DIR: path.join(libDir, 'npm', '.bin'),
        CHROME_EXTENSIONS_DIR: getExtensionsDir(),
    };
}

/**
 * Install a Chrome extension with caching support.
 *
 * This is the main entry point for extension installer hooks. It handles:
 * - Checking for cached extension metadata
 * - Installing the extension if not cached
 * - Writing cache file for future runs
 *
 * @param {Object} extension - Extension metadata object
 * @param {string} extension.webstore_id - Chrome Web Store extension ID
 * @param {string} extension.name - Human-readable extension name (used for cache file)
 * @param {Object} [options] - Options
 * @param {string} [options.extensionsDir] - Override extensions directory
 * @param {boolean} [options.noCache=false] - Ignore existing cached metadata
 * @param {boolean} [options.quiet=false] - Suppress info logging
 * @returns {Promise<Object|null>} - Installed extension metadata or null on failure
 */
async function installExtensionWithCache(extension, options = {}) {
    const {
        extensionsDir = getExtensionsDir(),
        quiet = false,
        noCache = false,
    } = options;

    const cacheFile = path.join(extensionsDir, `${extension.name}.extension.json`);

    // Check if extension is already cached and valid
    if (!noCache && fs.existsSync(cacheFile)) {
        try {
            const cached = JSON.parse(fs.readFileSync(cacheFile, 'utf-8'));
            const manifestPath = path.join(cached.unpacked_path, 'manifest.json');

            if (fs.existsSync(manifestPath)) {
                if (!quiet) {
                    console.log(`[*] ${extension.name} extension already installed (using cache)`);
                }
                return cached;
            }
        } catch (e) {
            // Cache file corrupted, re-install
            console.warn(`[⚠️] Extension cache corrupted for ${extension.name}, re-installing...`);
        }
    }

    // Install extension
    if (!quiet) {
        console.log(`[*] Installing ${extension.name} extension...`);
    }

    const installedExt = await loadOrInstallExtension(extension, extensionsDir, noCache);

    if (!installedExt?.version) {
        console.error(`[❌] Failed to install ${extension.name} extension`);
        return null;
    }

    // Write cache file
    try {
        await fs.promises.mkdir(extensionsDir, { recursive: true });
        await fs.promises.writeFile(cacheFile, JSON.stringify(installedExt, null, 2));
        if (!quiet) {
            console.log(`[+] Extension metadata written to ${cacheFile}`);
        }
    } catch (e) {
        console.warn(`[⚠️] Failed to write cache file: ${e.message}`);
    }

    if (!quiet) {
        console.log(`[+] ${extension.name} extension installed`);
    }

    return installedExt;
}

// ============================================================================
// Snapshot Hook Utilities (for CDP-based plugins like ssl, responses, dns)
// ============================================================================

const CHROME_SESSION_FILES = Object.freeze({
    cdpUrl: 'cdp_url.txt',
    targetId: 'target_id.txt',
    chromePid: 'chrome.pid',
});

/**
 * Parse command line arguments into an object.
 * Handles --key=value and --flag formats.
 *
 * @returns {Object} - Parsed arguments object
 */
/**
 * Resolve the canonical marker/artifact paths for one crawl- or snapshot-level
 * Chrome session directory.
 *
 * The crawl-level session typically owns the long-lived browser markers
 * (`chrome.pid`, `cdp_url.txt`, `extensions.json`). Snapshot-level sessions
 * reuse the same schema and add per-tab markers such as `target_id.txt`,
 * `url.txt`, and `navigation.json`.
 *
 * @param {string} chromeSessionDir - Path to chrome session directory
 * @returns {{sessionDir: string, cdpFile: string, targetIdFile: string, chromePidFile: string, urlFile: string, navigationFile: string}}
 */
function getChromeSessionPaths(chromeSessionDir) {
    const sessionDir = path.resolve(chromeSessionDir);
    return {
        sessionDir,
        cdpFile: path.join(sessionDir, CHROME_SESSION_FILES.cdpUrl),
        targetIdFile: path.join(sessionDir, CHROME_SESSION_FILES.targetId),
        chromePidFile: path.join(sessionDir, CHROME_SESSION_FILES.chromePid),
        urlFile: path.join(sessionDir, 'url.txt'),
        navigationFile: path.join(sessionDir, 'navigation.json'),
    };
}

/**
 * Read and trim a text file value if it exists.
 *
 * @param {string} filePath - File path
 * @returns {string|null} - Trimmed file value or null
 */
function readSessionTextFile(filePath) {
    if (!fs.existsSync(filePath)) return null;
    const value = fs.readFileSync(filePath, 'utf8').trim();
    return value || null;
}

/**
 * Return all persisted marker/artifact files that should be cleaned together
 * when a session is determined to be stale.
 *
 * The list intentionally includes both readiness markers and navigation
 * byproducts. Leaving old `navigation.json` or `extensions.json` files behind
 * can trick later hooks/tests into believing a brand-new session has already
 * advanced further than it actually has.
 *
 * @param {string} chromeSessionDir - Path to chrome session directory
 * @returns {string[]} - Absolute file paths
 */
function getChromeSessionArtifactPaths(chromeSessionDir) {
    const { sessionDir, cdpFile, targetIdFile, chromePidFile } = getChromeSessionPaths(chromeSessionDir);
    return [
        cdpFile,
        targetIdFile,
        chromePidFile,
        path.join(sessionDir, 'url.txt'),
        path.join(sessionDir, 'navigation.json'),
        path.join(sessionDir, 'extensions.json'),
    ];
}

/**
 * Extract the debug port from a Chrome browser websocket endpoint.
 *
 * @param {string|null} cdpUrl - Browser websocket endpoint
 * @returns {number|null} - Parsed port or null
 */
function getChromeDebugPortFromCdpUrl(cdpUrl) {
    if (!cdpUrl) return null;
    try {
        const parsed = new URL(cdpUrl);
        const port = parseInt(parsed.port, 10);
        return Number.isFinite(port) && port > 0 ? port : null;
    } catch (e) {
        const match = cdpUrl.match(/:(\d+)\/devtools\//);
        if (!match) return null;
        const port = parseInt(match[1], 10);
        return Number.isFinite(port) && port > 0 ? port : null;
    }
}

/**
 * Convert a Chrome websocket endpoint into the corresponding browser-server URL.
 *
 * ArchiveBox persists browser websocket endpoints in `cdp_url.txt`, while
 * tools such as `single-file-cli` expect an HTTP(S) browser-server base URL.
 * Keeping the translation here avoids re-implementing URL/port parsing in
 * Python helpers.
 *
 * @param {string|null} cdpUrl - Browser websocket or HTTP endpoint
 * @returns {string|null} - HTTP(S) browser-server URL or null if invalid
 */
function getBrowserCdpUrlFromCdpUrl(cdpUrl) {
    if (!cdpUrl) return null;

    try {
        const endpoint = new URL(cdpUrl);
        if (endpoint.protocol === 'http:' || endpoint.protocol === 'https:') {
            endpoint.pathname = '';
            endpoint.search = '';
            endpoint.hash = '';
            return endpoint.toString().replace(/\/+$/, '');
        }
        if (endpoint.protocol !== 'ws:' && endpoint.protocol !== 'wss:') {
            return null;
        }
        endpoint.protocol = endpoint.protocol === 'wss:' ? 'https:' : 'http:';
        endpoint.pathname = '';
        endpoint.search = '';
        endpoint.hash = '';
        return endpoint.toString().replace(/\/+$/, '');
    } catch (error) {
        return null;
    }
}

function getPuppeteerConnectOptionsForCdpUrl(cdpUrl) {
    if (!cdpUrl) {
        throw new Error('Missing CDP URL');
    }

    try {
        const endpoint = new URL(cdpUrl);
        if (endpoint.protocol === 'http:' || endpoint.protocol === 'https:') {
            return { browserURL: getBrowserCdpUrlFromCdpUrl(cdpUrl) || cdpUrl };
        }
        if (endpoint.protocol === 'ws:' || endpoint.protocol === 'wss:') {
            return { browserWSEndpoint: cdpUrl };
        }
        throw new Error(`Invalid CDP URL protocol: ${endpoint.protocol}`);
    } catch (error) {
        if (error instanceof Error) {
            throw error;
        }
        throw new Error(`Invalid CDP URL: ${cdpUrl}`);
    }
}

async function connectToBrowserEndpoint(puppeteer, cdpUrl, connectOptions = {}) {
    return await puppeteer.connect({
        ...getPuppeteerConnectOptionsForCdpUrl(cdpUrl),
        ...connectOptions,
    });
}

async function withTimeout(promiseFactory, timeoutMs, timeoutMessage) {
    let timeoutHandle = null;
    try {
        return await Promise.race([
            promiseFactory(),
            new Promise((_, reject) => {
                timeoutHandle = setTimeout(() => reject(new Error(timeoutMessage)), timeoutMs);
            }),
        ]);
    } finally {
        if (timeoutHandle) {
            clearTimeout(timeoutHandle);
        }
    }
}

async function canConnectToChromeBrowser(cdpUrl, options = {}) {
    const {
        timeoutMs = 1500,
        puppeteer = resolvePuppeteerModule(),
    } = options;

    let browser = null;
    try {
        browser = await withTimeout(
            () => connectToBrowserEndpoint(puppeteer, cdpUrl, { defaultViewport: null }),
            timeoutMs,
            `Timed out connecting to browser at ${cdpUrl}`
        );
        return true;
    } catch (error) {
        return false;
    } finally {
        if (browser) {
            try {
                await browser.disconnect();
            } catch (disconnectError) {}
        }
    }
}

/**
 * Inspect whether persisted session markers still correspond to a live attachable
 * Chrome session.
 *
 * This is the boundary between "files exist" and "the session is actually
 * reusable". It validates the saved websocket endpoint, optional target marker,
 * and pid state, then probes the DevTools port so callers can distinguish stale
 * leftovers from a healthy reusable session.
 *
 * @param {string} chromeSessionDir - Path to chrome session directory
 * @param {Object} [options={}] - Validation options
 * @param {boolean} [options.requireTargetId=false] - Require target ID marker to consider the session healthy
 * @param {number} [options.probeTimeoutMs=1500] - Timeout for probing the CDP endpoint
 * @param {boolean} [options.validateLiveness=true] - Probe whether the session is actually reusable
 * @param {Object} [options.puppeteer] - Puppeteer module for target-level liveness checks
 * @returns {Promise<{hasArtifacts: boolean, stale: boolean, state: Object, reason: string|null}>}
 */
async function inspectChromeSessionArtifacts(chromeSessionDir, options = {}) {
    const {
        requireTargetId = false,
        probeTimeoutMs = 1500,
        validateLiveness = true,
        processIsLocal = getEnv('CHROME_CDP_URL', '') ? false : getEnvBool('CHROME_IS_LOCAL', true),
        puppeteer = null,
    } = options;

    const artifactPaths = getChromeSessionArtifactPaths(chromeSessionDir);
    const hasArtifacts = artifactPaths.some(filePath => fs.existsSync(filePath));
    const sessionPaths = getChromeSessionPaths(chromeSessionDir);
    const cdpUrl = readSessionTextFile(sessionPaths.cdpFile);
    const targetId = readSessionTextFile(sessionPaths.targetIdFile);
    const rawPid = readSessionTextFile(sessionPaths.chromePidFile);
    const parsedPid = rawPid ? parseInt(rawPid, 10) : NaN;
    const pid = Number.isFinite(parsedPid) && parsedPid > 0 ? parsedPid : null;
    const state = {
        sessionDir: sessionPaths.sessionDir,
        cdpUrl,
        targetId,
        pid,
        extensions: readExtensionsMetadata(chromeSessionDir),
    };

    if (!hasArtifacts) {
        return { hasArtifacts: false, stale: false, state, reason: null };
    }

    if (!state.cdpUrl) {
        return { hasArtifacts: true, stale: true, state, reason: 'missing cdp_url.txt' };
    }

    if (requireTargetId && !state.targetId) {
        return { hasArtifacts: true, stale: true, state, reason: 'missing target_id.txt' };
    }

    if (!validateLiveness) {
        return { hasArtifacts: true, stale: false, state, reason: null };
    }

    if (requireTargetId && state.targetId) {
        let browser = null;
        try {
            const puppeteerModule = puppeteer || resolvePuppeteerModule();
            browser = await withTimeout(
                () => connectToBrowserEndpoint(puppeteerModule, state.cdpUrl, { defaultViewport: null }),
                probeTimeoutMs,
                `Timed out connecting to browser at ${state.cdpUrl}`
            );
            const page = await resolvePageByTargetId(browser, state.targetId, probeTimeoutMs);
            if (!page) {
                return {
                    hasArtifacts: true,
                    stale: true,
                    state,
                    reason: `target ${state.targetId} not found`,
                };
            }
            return { hasArtifacts: true, stale: false, state, reason: null };
        } catch (error) {
            return {
                hasArtifacts: true,
                stale: true,
                state,
                reason: error?.message || `cdp unreachable at ${state.cdpUrl}`,
            };
        } finally {
            if (browser) {
                try {
                    await browser.disconnect();
                } catch (disconnectError) {}
            }
        }
    }

    if (processIsLocal) {
        const debugPort = getChromeDebugPortFromCdpUrl(state.cdpUrl);
        if (!debugPort) {
            return { hasArtifacts: true, stale: true, state, reason: `invalid cdp url: ${state.cdpUrl}` };
        }

        try {
            await waitForDebugPort(debugPort, probeTimeoutMs);
            return { hasArtifacts: true, stale: false, state, reason: null };
        } catch (error) {
            return {
                hasArtifacts: true,
                stale: true,
                state,
                reason: `cdp unreachable on port ${debugPort}: ${error.message}`,
            };
        }
    }

    if (await canConnectToChromeBrowser(state.cdpUrl, { timeoutMs: probeTimeoutMs })) {
        return { hasArtifacts: true, stale: false, state, reason: null };
    }

    return {
        hasArtifacts: true,
        stale: true,
        state,
        reason: `cdp unreachable at ${state.cdpUrl}`,
    };
}

/**
 * Delete stale marker files for a session directory while leaving healthy ones
 * intact.
 *
 * This should be used before reusing a crawl/snapshot chrome directory. It is
 * safer than blindly unlinking only one file because the readiness lifecycle is
 * multi-step and stale markers tend to cluster.
 *
 * @param {string} chromeSessionDir - Path to chrome session directory
 * @param {Object} [options={}] - Validation options
 * @param {boolean} [options.requireTargetId=false] - Require target ID marker to consider the session healthy
 * @param {number} [options.probeTimeoutMs=1500] - Timeout for probing the CDP endpoint
 * @returns {Promise<{hasArtifacts: boolean, stale: boolean, state: Object, reason: string|null, cleanedFiles: string[]}>}
 */
async function cleanupStaleChromeSessionArtifacts(chromeSessionDir, options = {}) {
    const inspection = await inspectChromeSessionArtifacts(chromeSessionDir, options);
    const cleanedFiles = [];

    if (!inspection.stale) {
        return { ...inspection, cleanedFiles };
    }

    for (const filePath of getChromeSessionArtifactPaths(chromeSessionDir)) {
        if (!fs.existsSync(filePath)) continue;
        try {
            fs.unlinkSync(filePath);
            cleanedFiles.push(filePath);
        } catch (error) {}
    }

    return { ...inspection, cleanedFiles };
}

/**
 * Wait for the persisted marker state to contain the required fields.
 *
 * This waits for persisted session markers and can optionally require that the
 * published browser endpoint is actually CDP-connectable before succeeding.
 *
 * @param {string} chromeSessionDir - Path to chrome session directory
 * @param {Object} [options={}] - Wait/validation options
 * @param {number} [options.timeoutMs=60000] - Timeout in milliseconds
 * @param {number} [options.intervalMs=100] - Poll interval in milliseconds
 * @param {boolean} [options.requireTargetId=false] - Require target ID marker
 * @param {boolean} [options.requireExtensionsLoaded=false] - Require extensions.json to be available and parseable
 * @param {boolean} [options.requireConnectable=false] - Require the browser endpoint to be CDP-connectable
 * @param {number} [options.probeTimeoutMs=min(intervalMs, 1000)] - Timeout for each CDP connectability probe
 * @param {Object} [options.puppeteer] - Puppeteer module for target-level connectability checks
 * @returns {Promise<{sessionDir: string, cdpUrl: string|null, targetId: string|null, pid: number|null, extensions: Array<Object>|null}|null>}
 */
async function waitForChromeSessionState(chromeSessionDir, options = {}) {
    const {
        timeoutMs = 60000,
        intervalMs = 100,
        requireTargetId = false,
        requireExtensionsLoaded = false,
        requireConnectable = false,
        probeTimeoutMs = Math.min(Math.max(intervalMs, 100), 1000),
        puppeteer = null,
    } = options;
    const startTime = Date.now();

    while (Date.now() - startTime < timeoutMs) {
        const inspection = await inspectChromeSessionArtifacts(chromeSessionDir, {
            requireTargetId,
            validateLiveness: requireConnectable,
            probeTimeoutMs,
            puppeteer,
        });
        const state = inspection.state;
        if (
            state?.cdpUrl &&
            (!requireTargetId || state.targetId) &&
            (!requireExtensionsLoaded || state.extensions !== null) &&
            (!requireConnectable || !inspection.stale)
        ) {
            return state;
        }
        await new Promise(resolve => setTimeout(resolve, intervalMs));
    }

    return null;
}

/**
 * Ensure puppeteer module was passed in by callers.
 *
 * @param {Object} puppeteer - Puppeteer module
 * @param {string} callerName - Caller function name for errors
 * @returns {Object} - Puppeteer module
 * @throws {Error} - If puppeteer is missing
 */
function requirePuppeteerModule(puppeteer, callerName) {
    if (!puppeteer) {
        throw new Error(`puppeteer module must be passed to ${callerName}()`);
    }
    return puppeteer;
}

/**
 * Resolve puppeteer module from installed dependencies.
 *
 * @returns {Object} - Loaded puppeteer module
 * @throws {Error} - If no puppeteer package is installed
 */
function resolvePuppeteerModule() {
    for (const moduleName of ['puppeteer-core', 'puppeteer']) {
        try {
            return require(moduleName);
        } catch (e) {}
    }
    throw new Error('Missing puppeteer dependency (need puppeteer-core or puppeteer)');
}

async function waitForChromeLaunchPrerequisites(options = {}) {
    const {
        requireLocalBinary = true,
        timeoutMs = Math.max(getEnvInt('CHROME_TIMEOUT', 60) * 1000, 300000),
        initialIntervalMs = 100,
        maxIntervalMs = 1000,
    } = options;

    const startedAt = Date.now();
    let intervalMs = initialIntervalMs;
    let lastPuppeteerError = '';
    let lastBinaryError = '';

    while (Date.now() - startedAt < timeoutMs) {
        let puppeteer = null;
        let binary = null;

        try {
            puppeteer = resolvePuppeteerModule();
            lastPuppeteerError = '';
        } catch (error) {
            lastPuppeteerError = error.message;
        }

        if (requireLocalBinary) {
            binary = findChromium();
            if (!binary) {
                lastBinaryError = 'Chromium binary not found yet';
            } else {
                lastBinaryError = '';
            }
        }

        if (puppeteer && (!requireLocalBinary || binary)) {
            return { puppeteer, binary };
        }

        await sleep(intervalMs);
        intervalMs = Math.min(maxIntervalMs, Math.round(intervalMs * 1.5));
    }

    const details = [lastPuppeteerError, lastBinaryError].filter(Boolean).join('; ');
    throw new Error(
        details
            ? `Timed out waiting for Chrome launch prerequisites: ${details}`
            : 'Timed out waiting for Chrome launch prerequisites'
    );
}

/**
 * Connect to a running browser, run an operation, and always disconnect.
 *
 * @param {Object} options - Connection options
 * @param {Object} options.puppeteer - Puppeteer module
 * @param {string} options.browserWSEndpoint - Browser websocket endpoint
 * @param {Object} [options.connectOptions={}] - Additional puppeteer connect options
 * @param {Function} operation - Async callback receiving the browser
 * @returns {Promise<*>} - Operation return value
 */
async function withConnectedBrowser(options, operation) {
    const {
        puppeteer,
        browserWSEndpoint,
        browserURL,
        cdpUrl,
        connectOptions = {},
    } = options;

    const endpoint = browserURL || browserWSEndpoint || cdpUrl;
    const browser = await connectToBrowserEndpoint(puppeteer, endpoint, connectOptions);
    try {
        return await operation(browser);
    } finally {
        await browser.disconnect();
    }
}

/**
 * Configure Chrome's download behavior over the live CDP session.
 *
 * This is the supported way to set the downloads directory for ArchiveBox's
 * Chrome lifecycle. Call it after the browser is reachable but before crawl
 * readiness is published so later snapshot hooks inherit a fully-configured
 * browser without needing to mutate on-disk profile `Preferences`.
 *
 * @param {Object} options - Download behavior options
 * @param {Object} options.browser - Connected puppeteer browser instance
 * @param {string} options.downloadPath - Directory to save downloads in
 * @returns {Promise<boolean>} - True if configuration succeeded
 */
async function setBrowserDownloadBehavior(options = {}) {
    const {
        browser,
        page,
        downloadPath,
    } = options;

    if (!browser && !page) {
        throw new Error('setBrowserDownloadBehavior requires a browser or page');
    }
    if (!downloadPath) {
        throw new Error('setBrowserDownloadBehavior requires downloadPath');
    }

    await fs.promises.mkdir(downloadPath, { recursive: true });
    const sessionTarget = page ? page.target() : browser.target();
    const session = await sessionTarget.createCDPSession();

    // Keep the CDP session alive for the lifetime of the caller's browser/page
    // connection. Extension-driven downloads regress if we detach immediately
    // after configuring download behavior.
    try {
        await session.send('Browser.setDownloadBehavior', {
            behavior: 'allow',
            downloadPath,
        });
        console.error(`[+] Configured Chrome download directory via CDP: ${downloadPath}`);
        return true;
    } catch (browserError) {
        try {
            await session.send('Page.setDownloadBehavior', {
                behavior: 'allow',
                downloadPath,
            });
            console.error(`[+] Configured Chrome download directory via CDP: ${downloadPath}`);
            return true;
        } catch (pageError) {
            throw new Error(
                `Browser.setDownloadBehavior failed: ${browserError.message}; ` +
                `Page.setDownloadBehavior failed: ${pageError.message}`
            );
        }
    }
}

function getTargetIdFromTarget(target) {
    if (!target) return null;
    return target._targetId || target._targetInfo?.targetId || null;
}

function getTargetIdFromPage(page) {
    if (!page || typeof page.target !== 'function') return null;
    try {
        return getTargetIdFromTarget(page.target());
    } catch (error) {
        return null;
    }
}

async function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

async function cleanupLaunchArtifacts(outputDir, chromePid = null) {
    if (chromePid) {
        try {
            await killChrome(chromePid, outputDir);
        } catch (error) {}
    }

    try {
        await cleanupStaleChromeSessionArtifacts(outputDir, { probeTimeoutMs: 250 });
    } catch (error) {}
}

/**
 * Verify that a freshly launched browser survives long enough to be considered
 * stable for downstream hooks.
 *
 * This is stronger than "debug port opened once". It waits through the fragile
 * startup window and proves the websocket is attachable with Puppeteer.
 *
 * It must stay strictly earlier than crawl-level extension discovery. The
 * caller is responsible for inspecting extension targets and later writing
 * `extensions.json`; waiting for that file here would deadlock the launch flow.
 *
 * @param {Object} options - Verification options
 * @param {number} options.chromePid - Spawned Chrome PID
 * @param {string} options.cdpUrl - Browser websocket endpoint
 * @param {boolean} [options.headless=true] - Whether browser is headless
 * @param {string[]} [options.extensionPaths=[]] - Extension paths loaded at launch
 * @returns {Promise<void>}
 */
async function verifyStableChromiumSession(options = {}) {
    const {
        chromePid,
        cdpUrl,
        headless = true,
        extensionPaths = [],
    } = options;

    const hasExtensions = extensionPaths.length > 0;
    const settleMs = getEnvInt('CHROME_LAUNCH_SETTLE_MS', hasExtensions ? 1000 : 250);
    const stableMs = getEnvInt('CHROME_LAUNCH_STABILITY_MS', hasExtensions ? 2500 : 750);

    // A ready DevTools websocket is not enough on its own. Chromium sometimes
    // binds the port and then dies moments later while still finishing native
    // startup work. This verification step defines the stricter contract that
    // downstream hooks rely on:
    // - the spawned PID is still alive after a short settle delay
    // - a real CDP client can connect
    // - there is a usable initial page target
    // - the process stays alive for a brief post-connect stability window
    //
    // If any of those checks fail we classify it as an early startup failure,
    // not a successful launch. The outer launch loop can then decide whether
    // that failure is transient enough to retry.
    if (settleMs > 0) {
        await sleep(settleMs);
    }

    if (!chromePid || !isProcessAlive(chromePid)) {
        throw new Error(
            hasExtensions && headless
                ? 'Chromium exited during headless extension startup'
                : 'Chromium exited during startup'
        );
    }

    let browser = null;
    try {
        const puppeteer = resolvePuppeteerModule();
        browser = await connectToBrowserEndpoint(puppeteer, cdpUrl, {
            defaultViewport: null,
        });
        await waitForBrowserPageReady({
            browser,
            timeoutMs: getEnvInt('CHROME_PAGE_READY_TIMEOUT_MS', 10000),
            requireAboutBlank: true,
            createPageIfMissing: true,
        });
    } catch (error) {
        throw new Error(`Chromium CDP session not stable after startup: ${error.message}`);
    } finally {
        if (browser) {
            try {
                await browser.disconnect();
            } catch (disconnectError) {}
        }
    }

    const deadline = Date.now() + stableMs;
    while (Date.now() < deadline) {
        if (!isProcessAlive(chromePid)) {
            throw new Error(
                hasExtensions && headless
                    ? 'Chromium exited after opening the debug port during headless extension startup'
                    : 'Chromium exited after opening the debug port'
            );
        }
        await sleep(200);
    }
}

async function waitForBrowserPageReady(options = {}) {
    const {
        browser = null,
        puppeteer = null,
        cdpUrl = null,
        timeoutMs = 10000,
        requireAboutBlank = false,
        createPageIfMissing = true,
    } = options;

    let ownedBrowser = null;
    let connectedBrowser = browser;
    let createdProbePage = false;
    let lastError = null;
    const deadline = Date.now() + Math.max(timeoutMs, 0);

    if (!connectedBrowser) {
        const puppeteerModule = requirePuppeteerModule(puppeteer, 'waitForBrowserPageReady');
        connectedBrowser = await connectToBrowserEndpoint(puppeteerModule, cdpUrl, {
            defaultViewport: null,
        });
        ownedBrowser = connectedBrowser;
    }

    try {
        while (Date.now() <= deadline) {
            let pages = [];
            try {
                pages = await connectedBrowser.pages();
            } catch (error) {
                lastError = error;
            }

            let page = pages.find(candidate => candidate && candidate.url() === 'about:blank') || pages[0] || null;
            if ((!page || (requireAboutBlank && page.url() !== 'about:blank')) && createPageIfMissing && !createdProbePage) {
                try {
                    page = await connectedBrowser.newPage();
                    createdProbePage = true;
                } catch (error) {
                    lastError = error;
                }
            }

            if (page) {
                try {
                    const url = page.url();
                    if (requireAboutBlank && url !== 'about:blank') {
                        lastError = new Error(`Expected about:blank probe page, found ${url || '<empty>'}`);
                    } else {
                        const title = await page.title();
                        const targetId = getTargetIdFromPage(page);
                        if (!targetId) {
                            throw new Error('Missing target ID for probe page');
                        }
                        return { browser: connectedBrowser, page, targetId, url, title };
                    }
                } catch (error) {
                    lastError = error;
                }
            } else if (!lastError) {
                lastError = new Error('No page targets available yet');
            }

            await sleep(100);
        }

        throw new Error(lastError?.message || 'Timed out waiting for a usable Chrome page');
    } finally {
        if (ownedBrowser) {
            try {
                await ownedBrowser.disconnect();
            } catch (disconnectError) {}
        }
    }
}

async function resolvePageByTargetId(browser, targetId, timeoutMs = 0) {
    const deadline = Date.now() + Math.max(timeoutMs, 0);

    while (true) {
        const targets = browser.targets();
        const target = targets.find(candidate => getTargetIdFromTarget(candidate) === targetId);
        if (target) {
            try {
                const page = await target.page();
                if (page) {
                    return page;
                }
            } catch (error) {}
        }

        const pages = await browser.pages();
        const pageMatch = pages.find(page => getTargetIdFromPage(page) === targetId);
        if (pageMatch) {
            return pageMatch;
        }

        if (Date.now() >= deadline) {
            return null;
        }

        await sleep(100);
    }
}

/**
 * Resolve a live browser-server URL from an already-published session dir.
 *
 * This is the browser-level analogue to `connectToPage(...)`: it waits for the
 * marker contract, verifies the underlying session is still reusable, then
 * returns the HTTP(S) base URL expected by browser-scoped tools.
 *
 * @param {string} [chromeSessionDir='../chrome'] - Session directory to inspect
 * @param {Object} [options={}] - Resolution options
 * @param {number} [options.timeoutMs=60000] - Timeout waiting for markers
 * @param {boolean} [options.requireTargetId=true] - Require target_id.txt
 * @returns {Promise<string>} - Browser-server URL
 * @throws {Error} - If no reusable Chrome session is available
 */
async function getBrowserCdpUrl(chromeSessionDir = '../chrome', options = {}) {
    const {
        timeoutMs = 60000,
        requireTargetId = true,
    } = options;
    const processIsLocal = options.processIsLocal ?? (getEnv('CHROME_CDP_URL', '') ? false : getEnvBool('CHROME_IS_LOCAL', true));

    const state = await waitForChromeSessionState(chromeSessionDir, {
        timeoutMs,
        requireTargetId,
    });
    if (!state?.cdpUrl) {
        throw new Error(CHROME_SESSION_REQUIRED_ERROR);
    }

    const inspection = await inspectChromeSessionArtifacts(chromeSessionDir, {
        requireTargetId,
        probeTimeoutMs: Math.min(Math.max(timeoutMs, 250), 2000),
        processIsLocal,
    });
    if (inspection.stale || !inspection.state?.cdpUrl) {
        throw new Error(CHROME_SESSION_REQUIRED_ERROR);
    }

    const browserServerUrl = getBrowserCdpUrlFromCdpUrl(inspection.state.cdpUrl);
    if (!browserServerUrl) {
        throw new Error('Invalid CDP URL in chrome session');
    }
    return browserServerUrl;
}

/**
 * Open a blank page target inside an existing crawl-level browser session.
 *
 * This helper only asks DevTools to create the target and returns its runtime
 * `targetId`. Persisting snapshot-level markers such as `target_id.txt`,
 * `cdp_url.txt`, or copied `extensions.json` remains the responsibility of the
 * snapshot tab hook.
 *
 * @param {Object} options - Tab open options
 * @param {string} options.cdpUrl - Browser CDP websocket URL
 * @param {Object} options.puppeteer - Puppeteer module
 * @returns {Promise<{targetId: string}>}
 */
async function openTabInChromeSession(options = {}) {
    const {
        cdpUrl,
        puppeteer,
        timeoutMs = 10000,
        intervalMs = 250,
    } = options;
    if (!cdpUrl) {
        throw new Error(CHROME_SESSION_REQUIRED_ERROR);
    }
    const puppeteerModule = requirePuppeteerModule(puppeteer, 'openTabInChromeSession');
    const deadline = Date.now() + Math.max(timeoutMs, 0);
    let lastError = null;

    while (Date.now() <= deadline) {
        try {
            return await withConnectedBrowser(
                {
                    puppeteer: puppeteerModule,
                    cdpUrl,
                    connectOptions: { defaultViewport: null },
                },
                async (browser) => {
                    const remainingMs = Math.max(1000, Math.min(5000, deadline - Date.now()));
                    const page = await withTimeout(
                        () => browser.newPage(),
                        remainingMs,
                        `Timed out creating new page after ${remainingMs}ms`
                    );
                    await withTimeout(
                        () => page.title(),
                        remainingMs,
                        `Timed out probing new page after ${remainingMs}ms`
                    );
                    const targetId = getTargetIdFromPage(page);
                    if (!targetId) {
                        throw new Error('Failed to resolve target ID for new tab');
                    }
                    return { targetId };
                }
            );
        } catch (error) {
            lastError = error;
            if (Date.now() >= deadline) {
                break;
            }
            await sleep(intervalMs);
        }
    }

    throw lastError || new Error('Failed to open a new Chrome tab');
}

/**
 * Close a tab by target ID in an existing Chrome session.
 *
 * @param {Object} options - Tab close options
 * @param {string} options.cdpUrl - Browser CDP websocket URL
 * @param {string} options.targetId - Target ID to close
 * @param {Object} options.puppeteer - Puppeteer module
 * @returns {Promise<boolean>} - True if a tab was found and closed
 */
async function closeTabInChromeSession(options = {}) {
    const { cdpUrl, targetId, puppeteer } = options;
    if (!cdpUrl || !targetId) {
        return false;
    }
    const puppeteerModule = requirePuppeteerModule(puppeteer, 'closeTabInChromeSession');

    return withConnectedBrowser(
        {
            puppeteer: puppeteerModule,
            cdpUrl,
            connectOptions: { defaultViewport: null },
        },
        async (browser) => {
            const page = await resolvePageByTargetId(browser, targetId, 1000);
            if (!page) {
                return false;
            }
            await page.close();
            return true;
        }
    );
}

/**
 * Attach to a persisted session directory and resolve the corresponding page.
 *
 * This is the high-level handoff from filesystem readiness markers to a live
 * Puppeteer page object. On success it transfers browser ownership to the
 * caller; on failure before handoff it disconnects immediately so callers do
 * not inherit half-initialized state.
 *
 * @param {Object} options - Connection options
 * @param {string} [options.chromeSessionDir='../chrome'] - Path to chrome session directory
 * @param {number} [options.timeoutMs=60000] - Timeout for waiting
 * @param {boolean} [options.requireTargetId=true] - Require target_id.txt in session dir
 * @param {boolean} [options.requireExtensionsLoaded=false] - Require extensions.json to be available and parseable
 * @param {boolean} [options.waitForNavigationComplete=false] - Wait for navigation.json success before attaching
 * @param {number} [options.pageLoadTimeoutMs=timeoutMs] - Timeout for navigation.json readiness
 * @param {number} [options.postLoadDelayMs=0] - Additional delay after successful navigation
 * @param {number} [options.missingTargetGraceMs=3000] - How long to tolerate a missing published target before failing
 * @param {Object} options.puppeteer - Puppeteer module
 * @returns {Promise<Object>} - { browser, page, cdpSession, targetId, cdpUrl, extensions }
 * @throws {Error} - If connection fails or page not found
 */
async function connectToPage(options = {}) {
    const {
        chromeSessionDir = '../chrome',
        timeoutMs = 60000,
        requireTargetId = true,
        requireExtensionsLoaded = false,
        waitForNavigationComplete: shouldWaitForNavigationComplete = false,
        pageLoadTimeoutMs = timeoutMs,
        postLoadDelayMs = 0,
        missingTargetGraceMs = 3000,
        puppeteer,
    } = options;

    const resolvedPuppeteer = puppeteer || resolvePuppeteerModule();
    const initialInspection = await inspectChromeSessionArtifacts(chromeSessionDir, {
        requireTargetId,
        validateLiveness: false,
    });
    if (!initialInspection.hasArtifacts) {
        throw new Error(CHROME_SESSION_REQUIRED_ERROR);
    }
    if (!initialInspection.state?.cdpUrl) {
        throw new Error(CHROME_SESSION_REQUIRED_ERROR);
    }
    getPuppeteerConnectOptionsForCdpUrl(initialInspection.state.cdpUrl);
    if (requireTargetId && !initialInspection.state?.targetId) {
        const sessionPaths = getChromeSessionPaths(chromeSessionDir);
        const hasLaterSnapshotMarkers = [
            sessionPaths.urlFile,
            sessionPaths.navigationFile,
        ].some(filePath => fs.existsSync(filePath));
        if (hasLaterSnapshotMarkers) {
            throw new Error('No target_id.txt found');
        }
    }

    if (shouldWaitForNavigationComplete) {
        await waitForNavigationComplete(chromeSessionDir, pageLoadTimeoutMs, postLoadDelayMs);
    }

    const deadline = Date.now() + timeoutMs;
    let lastError = new Error(CHROME_SESSION_REQUIRED_ERROR);
    let missingTargetKey = null;
    let missingTargetSince = 0;
    const staleTargetGraceMs = Math.min(timeoutMs, Math.max(0, missingTargetGraceMs));

    while (Date.now() < deadline) {
        const remainingMs = Math.max(deadline - Date.now(), 0);
        const state = await waitForChromeSessionState(chromeSessionDir, {
            timeoutMs: Math.min(remainingMs, 500),
            intervalMs: 100,
            requireTargetId,
            requireExtensionsLoaded,
        });
        if (!state) {
            missingTargetKey = null;
            missingTargetSince = 0;
            if (Date.now() >= deadline) {
                break;
            }
            await sleep(100);
            continue;
        }

        const targetId = state.targetId;
        const browser = await connectToBrowserEndpoint(resolvedPuppeteer, state.cdpUrl, { defaultViewport: null })
            .catch((error) => {
                lastError = error instanceof Error ? error : new Error(String(error));
                return null;
            });

        if (!browser) {
            if (Date.now() >= deadline) break;
            await sleep(100);
            continue;
        }

        try {
            let page = null;

            if (targetId) {
                page = await resolvePageByTargetId(browser, targetId, Math.min(remainingMs, 1000));
                if (!page && requireTargetId) {
                    const currentTargetKey = `${state.cdpUrl}::${targetId}`;
                    const now = Date.now();
                    if (missingTargetKey !== currentTargetKey) {
                        missingTargetKey = currentTargetKey;
                        missingTargetSince = now;
                    } else if (now - missingTargetSince >= staleTargetGraceMs) {
                        const error = new Error(`Target ${targetId} not found in Chrome session`);
                        error.code = 'TARGET_NOT_FOUND_STABLE';
                        throw error;
                    }
                    throw new Error(`Target ${targetId} not found in Chrome session`);
                }
                missingTargetKey = null;
                missingTargetSince = 0;
            }

            const pages = await browser.pages();
            if (!page) {
                page = pages[pages.length - 1];
            }

            if (!page) {
                throw new Error('No page found in browser');
            }

            const cdpSession = await page.target().createCDPSession();
            await cdpSession.send('Target.setAutoAttach', {
                autoAttach: true,
                waitForDebuggerOnStart: false,
                flatten: true,
            });

            return {
                ...state,
                browser,
                page,
                cdpSession,
                targetId,
            };
        } catch (error) {
            lastError = error instanceof Error ? error : new Error(String(error));
            try {
                await browser.disconnect();
            } catch (disconnectError) {}
            if (lastError.code === 'TARGET_NOT_FOUND_STABLE') {
                break;
            }
        }

        if (Date.now() >= deadline) {
            break;
        }
        await sleep(100);
    }

    throw lastError;
}

function loadInstalledExtensionsFromCache(extensionsDir = getExtensionsDir()) {
    const installedExtensions = [];
    const extensionPaths = [];

    if (!fs.existsSync(extensionsDir)) {
        return { installedExtensions, extensionPaths };
    }

    for (const file of fs.readdirSync(extensionsDir)) {
        if (!file.endsWith('.extension.json')) continue;

        try {
            const extPath = path.join(extensionsDir, file);
            const extData = JSON.parse(fs.readFileSync(extPath, 'utf-8'));
            if (!extData.unpacked_path || !fs.existsSync(extData.unpacked_path)) continue;
            if (!extData.id) {
                extData.id = getExtensionId(extData.unpacked_path);
            }
            installedExtensions.push(extData);
            extensionPaths.push(extData.unpacked_path);
        } catch (error) {}
    }

    return { installedExtensions, extensionPaths };
}

function parseCookiesTxt(contents) {
    const cookies = [];
    let skipped = 0;

    for (const rawLine of contents.split(/\r?\n/)) {
        const line = rawLine.trim();
        if (!line) continue;

        let httpOnly = false;
        let dataLine = line;

        if (dataLine.startsWith('#HttpOnly_')) {
            httpOnly = true;
            dataLine = dataLine.slice('#HttpOnly_'.length);
        } else if (dataLine.startsWith('#')) {
            continue;
        }

        const parts = dataLine.split('\t');
        if (parts.length < 7) {
            skipped += 1;
            continue;
        }

        const [domainRaw, includeSubdomainsRaw, pathRaw, secureRaw, expiryRaw, name, value] = parts;
        if (!name || !domainRaw) {
            skipped += 1;
            continue;
        }

        const includeSubdomains = (includeSubdomainsRaw || '').toUpperCase() === 'TRUE';
        let domain = domainRaw;
        if (includeSubdomains && !domain.startsWith('.')) domain = `.${domain}`;
        if (!includeSubdomains && domain.startsWith('.')) domain = domain.slice(1);

        const cookie = {
            name,
            value,
            domain,
            path: pathRaw || '/',
            secure: (secureRaw || '').toUpperCase() === 'TRUE',
            httpOnly,
        };

        const expires = parseInt(expiryRaw, 10);
        if (!isNaN(expires) && expires > 0) {
            cookie.expires = expires;
        }

        cookies.push(cookie);
    }

    return { cookies, skipped };
}

async function importCookiesFromFile(browser, cookiesFile, userDataDir) {
    if (!cookiesFile) return;

    if (!fs.existsSync(cookiesFile)) {
        console.error(`[!] Cookies file not found: ${cookiesFile}`);
        return;
    }

    let contents = '';
    try {
        contents = fs.readFileSync(cookiesFile, 'utf-8');
    } catch (e) {
        console.error(`[!] Failed to read COOKIES_TXT_FILE: ${e.message}`);
        return;
    }

    const { cookies, skipped } = parseCookiesTxt(contents);
    if (cookies.length === 0) {
        console.error('[!] No cookies found to import');
        return;
    }

    console.error(`[*] Importing ${cookies.length} cookies from ${cookiesFile}...`);
    if (skipped) {
        console.error(`[*] Skipped ${skipped} malformed cookie line(s)`);
    }
    if (!userDataDir) {
        console.error('[!] CHROME_USER_DATA_DIR not set; cookies will not persist beyond this session');
    }

    const page = await browser.newPage();
    const client = await page.target().createCDPSession();
    await client.send('Network.enable');

    const chunkSize = 200;
    let imported = 0;
    for (let i = 0; i < cookies.length; i += chunkSize) {
        const chunk = cookies.slice(i, i + chunkSize);
        try {
            await client.send('Network.setCookies', { cookies: chunk });
            imported += chunk.length;
        } catch (e) {
            console.error(`[!] Failed to import cookies ${i + 1}-${i + chunk.length}: ${e.message}`);
        }
    }

    await page.close();
    console.error(`[+] Imported ${imported}/${cookies.length} cookies`);
}

async function waitForProcessExit(pid, timeoutMs = 5000, intervalMs = 100) {
    if (!pid) return true;
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
        if (!isProcessAlive(pid)) {
            return true;
        }
        await sleep(intervalMs);
    }
    return !isProcessAlive(pid);
}

async function waitForBrowserEndpointGone(cdpUrl, timeoutMs = 5000, intervalMs = 200) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
        if (!(await canConnectToChromeBrowser(cdpUrl, { timeoutMs: Math.min(intervalMs, 1000) }))) {
            return true;
        }
        await sleep(intervalMs);
    }
    return !(await canConnectToChromeBrowser(cdpUrl, { timeoutMs: Math.min(intervalMs, 1000) }));
}

async function closeBrowserInChromeSession(options = {}) {
    const {
        cdpUrl = null,
        pid = null,
        outputDir = null,
        puppeteer = resolvePuppeteerModule(),
        processIsLocal = getEnv('CHROME_CDP_URL', '') ? false : getEnvBool('CHROME_IS_LOCAL', true),
        forceKillTimeoutMs = getEnvInt('CHROME_CLOSE_TIMEOUT_MS', 5000),
    } = options;

    if (!cdpUrl && !(processIsLocal && pid)) {
        return false;
    }

    if (cdpUrl) {
        let browser = null;
        try {
            browser = await connectToBrowserEndpoint(puppeteer, cdpUrl, { defaultViewport: null });
            const session = await browser.target().createCDPSession();
            await session.send('Browser.close');
        } catch (error) {
            console.error(`[!] Browser.close failed: ${error.message}`);
        } finally {
            if (browser) {
                try {
                    await browser.disconnect();
                } catch (disconnectError) {}
            }
        }
    }

    const debugPort = cdpUrl ? getChromeDebugPortFromCdpUrl(cdpUrl) : null;
    let closed = false;
    if (processIsLocal && pid) {
        closed = await waitForProcessExit(pid, forceKillTimeoutMs);
        if (!closed) {
            closed = await killChrome(pid, outputDir);
        }
    } else if (cdpUrl) {
        closed = await waitForBrowserEndpointGone(cdpUrl, forceKillTimeoutMs);
        if (closed && debugPort) {
            const relatedPids = findChromeProcessesByPort(debugPort);
            if (relatedPids.length > 0) {
                closed = await killChrome(relatedPids[0], outputDir);
            }
        }
    }

    if (outputDir && closed) {
        try {
            await cleanupStaleChromeSessionArtifacts(outputDir, {
                processIsLocal,
                probeTimeoutMs: Math.min(Math.max(forceKillTimeoutMs, 250), 1000),
            });
        } catch (error) {}
    }

    return closed;
}

async function ensureChromeSession(options = {}) {
    const {
        outputDir = '.',
        puppeteer = resolvePuppeteerModule(),
        processIsLocal = getEnv('CHROME_CDP_URL', '') ? false : getEnvBool('CHROME_IS_LOCAL', true),
        cdpUrl = getEnv('CHROME_CDP_URL', ''),
        userDataDir = getEnv('CHROME_USER_DATA_DIR'),
        downloadsDir = getEnv('CHROME_DOWNLOADS_DIR'),
        cookiesFile = getEnv('COOKIES_TXT_FILE') || getEnv('COOKIES_FILE'),
        extensionsDir = getExtensionsDir(),
        timeoutMs = getEnvInt('CHROME_TIMEOUT', 60) * 1000,
        reuseExisting = !cdpUrl,
        binary = null,
    } = options;

    if (!fs.existsSync(outputDir)) {
        fs.mkdirSync(outputDir, { recursive: true });
    }

    const { installedExtensions, extensionPaths } = loadInstalledExtensionsFromCache(extensionsDir);

    const existingSession = await inspectChromeSessionArtifacts(outputDir, { processIsLocal });
    const reusingExplicitCdpUrl =
        Boolean(cdpUrl) &&
        existingSession.hasArtifacts &&
        !existingSession.stale &&
        existingSession.state?.cdpUrl === cdpUrl;

    if (reuseExisting && existingSession.hasArtifacts && !existingSession.stale && existingSession.state?.cdpUrl) {
        return {
            cdpUrl: existingSession.state.cdpUrl,
            pid: existingSession.state.pid,
            port: getChromeDebugPortFromCdpUrl(existingSession.state.cdpUrl),
            installedExtensions,
            extensionPaths,
            processIsLocal,
            reusedExisting: true,
            binary,
        };
    }

    if (!reusingExplicitCdpUrl && existingSession.hasArtifacts && existingSession.state?.cdpUrl) {
        try {
            await closeBrowserInChromeSession({
                cdpUrl: existingSession.state.cdpUrl,
                pid: existingSession.state.pid,
                outputDir,
                puppeteer,
                processIsLocal: Boolean(existingSession.state.pid),
            });
        } catch (error) {}
    }

    if (!reusingExplicitCdpUrl) {
        const staleSession = await cleanupStaleChromeSessionArtifacts(outputDir, {
            processIsLocal: existingSession.state?.pid ? true : processIsLocal,
        });
        if (staleSession.cleanedFiles.length === 0) {
            for (const filePath of getChromeSessionArtifactPaths(outputDir)) {
                if (!fs.existsSync(filePath)) continue;
                try {
                    fs.unlinkSync(filePath);
                } catch (error) {}
            }
        }
    }

    let resolvedBinary = binary;
    let resolvedPid = reusingExplicitCdpUrl && processIsLocal ? (existingSession.state?.pid || null) : null;
    let resolvedCdpUrl = reusingExplicitCdpUrl ? existingSession.state?.cdpUrl : cdpUrl;

    if (!resolvedCdpUrl) {
        if (!processIsLocal) {
            throw new Error('CHROME_IS_LOCAL=false requires CHROME_CDP_URL or an upstream published chrome session');
        }

        resolvedBinary = resolvedBinary || findChromium();
        if (!resolvedBinary) {
            throw new Error('Chromium binary not found');
        }

        const result = await launchChromium({
            binary: resolvedBinary,
            outputDir,
            userDataDir,
            extensionPaths,
        });
        if (!result.success) {
            throw new Error(result.error || 'Failed to launch Chromium');
        }

        resolvedPid = result.pid;
        resolvedCdpUrl = result.cdpUrl;
    }

    if (downloadsDir || cookiesFile || installedExtensions.length > 0) {
        let browser = null;
        try {
            browser = await connectToBrowserEndpoint(puppeteer, resolvedCdpUrl, { defaultViewport: null });

            if (installedExtensions.length > 0) {
                await loadAllExtensionsFromBrowser(browser, installedExtensions, timeoutMs);
            }

            if (downloadsDir) {
                await setBrowserDownloadBehavior({
                    browser,
                    downloadPath: downloadsDir,
                });
            }

            if (cookiesFile) {
                await importCookiesFromFile(browser, cookiesFile, userDataDir);
            }
        } finally {
            if (browser) {
                try {
                    await browser.disconnect();
                } catch (error) {}
            }
        }
    }

    await waitForBrowserPageReady({
        puppeteer,
        cdpUrl: resolvedCdpUrl,
        timeoutMs: getEnvInt('CHROME_PAGE_READY_TIMEOUT_MS', 10000),
        requireAboutBlank: true,
        createPageIfMissing: true,
    });

    if (processIsLocal && resolvedPid) {
        try {
            process.kill(resolvedPid, 0);
        } catch (error) {
            throw new Error(`Chrome process ${resolvedPid} exited during launch setup`);
        }

        await new Promise(resolve => setTimeout(resolve, 1000));
        try {
            process.kill(resolvedPid, 0);
        } catch (error) {
            throw new Error(`Chrome process ${resolvedPid} exited immediately after launch setup`);
        }
    }

    if (installedExtensions.length > 0) {
        fs.writeFileSync(
            path.join(outputDir, 'extensions.json'),
            JSON.stringify(installedExtensions, null, 2)
        );
    } else {
        try { fs.unlinkSync(path.join(outputDir, 'extensions.json')); } catch (error) {}
    }

    if (resolvedPid) {
        fs.writeFileSync(path.join(outputDir, 'chrome.pid'), String(resolvedPid));
    } else {
        try { fs.unlinkSync(path.join(outputDir, 'chrome.pid')); } catch (error) {}
    }
    fs.writeFileSync(path.join(outputDir, 'cdp_url.txt'), resolvedCdpUrl);

    return {
        cdpUrl: resolvedCdpUrl,
        pid: resolvedPid,
        port: getChromeDebugPortFromCdpUrl(resolvedCdpUrl),
        installedExtensions,
        extensionPaths,
        processIsLocal,
        reusedExisting: false,
        binary: resolvedBinary,
    };
}

/**
 * Wait for the snapshot navigation hook to publish a successful navigation result.
 *
 * This does not perform navigation by itself. It only watches the
 * `navigation.json` artifact emitted by `chrome_navigate` and optionally waits
 * a bit longer for late network work that should remain within the same
 * snapshot lifecycle.
 *
 * @param {string} chromeSessionDir - Path to chrome session directory
 * @param {number} [timeoutMs=120000] - Timeout in milliseconds
 * @param {number} [postLoadDelayMs=0] - Additional delay after successful navigation
 * @returns {Promise<Object>} - Parsed navigation state
 * @throws {Error} - If timeout waiting for navigation or navigation.json reports an error
 */
async function waitForNavigationComplete(chromeSessionDir, timeoutMs = 120000, postLoadDelayMs = 0) {
    const { navigationFile } = getChromeSessionPaths(chromeSessionDir);
    const pollInterval = 100;
    const deadline = Date.now() + timeoutMs;
    let lastParseError = null;

    while (Date.now() < deadline) {
        if (!fs.existsSync(navigationFile)) {
            await new Promise(resolve => setTimeout(resolve, pollInterval));
            continue;
        }

        try {
            const rawNavigationState = fs.readFileSync(navigationFile, 'utf8');
            if (!rawNavigationState.trim()) {
                throw new SyntaxError('navigation.json is empty');
            }
            const navigationState = JSON.parse(rawNavigationState);
            if (navigationState?.error) {
                throw new Error(navigationState.error);
            }

            if (postLoadDelayMs > 0) {
                await new Promise(resolve => setTimeout(resolve, postLoadDelayMs));
            }

            return navigationState;
        } catch (error) {
            if (error instanceof SyntaxError) {
                lastParseError = error;
                await new Promise(resolve => setTimeout(resolve, pollInterval));
                continue;
            }
            throw error;
        }
    }

    if (lastParseError) {
        throw new Error(`Timeout waiting for navigation (invalid navigation.json: ${lastParseError.message})`);
    }
    throw new Error('Timeout waiting for navigation (chrome_navigate did not complete)');
}

/**
 * Read all browser cookies from a running Chrome CDP debug port.
 * Uses existing CDP bootstrap helpers and puppeteer connection logic.
 *
 * @param {number} port - Chrome remote debugging port
 * @param {Object} [options={}] - Optional settings
 * @param {number} [options.timeoutMs=10000] - Timeout waiting for debug port
 * @returns {Promise<Array<Object>>} - Array of cookie objects
 */
async function getCookiesViaCdp(port, options = {}) {
    const timeoutMs = options.timeoutMs || getEnvInt('CDP_COOKIE_TIMEOUT_MS', 10000);
    const versionInfo = await waitForDebugPort(port, timeoutMs);
    const browserWSEndpoint = versionInfo?.webSocketDebuggerUrl;
    if (!browserWSEndpoint) {
        throw new Error(`No webSocketDebuggerUrl from Chrome debug port ${port}`);
    }
    const puppeteerModule = resolvePuppeteerModule();

    return withConnectedBrowser(
        {
            puppeteer: puppeteerModule,
            browserWSEndpoint,
        },
        async (browser) => {
            const session = await browser.target().createCDPSession();
            const result = await session.send('Storage.getCookies');
            return result?.cookies || [];
        }
    );
}

// Export all functions
module.exports = {
    // Environment helpers
    getEnv,
    getEnvBool,
    getEnvInt,
    getEnvArray,
    parseResolution,
    // PID file management
    writePidWithMtime,
    writeCmdScript,
    acquireSessionLock,
    // Port management
    findFreePort,
    waitForDebugPort,
    // Zombie cleanup
    killZombieChrome,
    // Chrome launching
    launchChromium,
    killChrome,
    // Chromium install
    installChromium,
    installPuppeteerCore,
    // Chromium binary finding
    findChromium,
    findAnyChromiumBinary,
    // Extension utilities
    getExtensionId,
    loadExtensionManifest,
    installExtension,
    loadOrInstallExtension,
    isTargetExtension,
    loadExtensionFromTarget,
    installAllExtensions,
    loadAllExtensionsFromBrowser,
    waitForExtensionTargetHandle,
    // New puppeteer best-practices helpers
    resolvePuppeteerModule,
    connectToBrowserEndpoint,
    withConnectedBrowser,
    getExtensionPaths,
    waitForExtensionTarget,
    getExtensionTargets,
    findExtensionMetadataByName,
    loadInstalledExtensionsFromCache,
    importCookiesFromFile,
    ensureChromeSession,
    // Shared path utilities (single source of truth for Python/JS)
    getMachineType,
    getLibDir,
    getNodeModulesDir,
    getExtensionsDir,
    getTestEnv,
    // Shared extension installer utilities
    installExtensionWithCache,
    // Deprecated - use enableExtensions option instead
    getExtensionLaunchArgs,
    // Snapshot hook utilities (for CDP-based plugins)
    parseArgs,
    inspectChromeSessionArtifacts,
    cleanupStaleChromeSessionArtifacts,
    waitForChromeSessionState,
    waitForChromeLaunchPrerequisites,
    getBrowserCdpUrl,
    openTabInChromeSession,
    closeTabInChromeSession,
    closeBrowserInChromeSession,
    getTargetIdFromTarget,
    getTargetIdFromPage,
    connectToPage,
    waitForNavigationComplete,
    setBrowserDownloadBehavior,
    getCookiesViaCdp,
};

// CLI usage
if (require.main === module) {
    const args = process.argv.slice(2);

    if (args.length === 0) {
        console.log('Usage: chrome_utils.js <command> [args...]');
        console.log('');
        console.log('Commands:');
        console.log('  findChromium              Find Chromium binary');
        console.log('  installChromium           Install Chromium via @puppeteer/browsers');
        console.log('  installPuppeteerCore      Install puppeteer-core npm package');
        console.log('  launchChromium            Launch Chrome with CDP debugging');
        console.log('  getCookiesViaCdp <port>  Read browser cookies via CDP port');
        console.log('  getBrowserCdpUrl      Resolve browser-server URL from session dir');
        console.log('  killChrome <pid>          Kill Chrome process by PID');
        console.log('  killZombieChrome          Clean up zombie Chrome processes');
        console.log('');
        console.log('  getMachineType            Get machine type (e.g., x86_64-linux)');
        console.log('  getLibDir                 Get LIB_DIR path');
        console.log('  getNodeModulesDir         Get NODE_MODULES_DIR path');
        console.log('  getExtensionsDir          Get Chrome extensions directory');
        console.log('  getTestEnv                Get all paths as JSON (for tests)');
        console.log('');
        console.log('  getExtensionId <path>     Get extension ID from unpacked path');
        console.log('  loadExtensionManifest     Load extension manifest.json');
        console.log('  loadOrInstallExtension    Load or install an extension');
        console.log('  installExtensionWithCache Install extension with caching');
        console.log('');
        console.log('Environment variables:');
        console.log('  SNAP_DIR                  Base snapshot directory');
        console.log('  CRAWL_DIR                 Base crawl directory');
        console.log('  PERSONAS_DIR              Personas directory');
        console.log('  LIB_DIR                   Library directory (computed if not set)');
        console.log('  MACHINE_TYPE              Machine type override');
        console.log('  NODE_MODULES_DIR          Node modules directory');
        console.log('  CHROME_BINARY             Chrome binary path');
        console.log('  CHROME_EXTENSIONS_DIR     Extensions directory');
        process.exit(1);
    }

    const [command, ...commandArgs] = args;

    (async () => {
        try {
            switch (command) {
                case 'findChromium': {
                    const binary = findChromium();
                    if (binary) {
                        console.log(binary);
                    } else {
                        console.error('Chromium binary not found');
                        process.exit(1);
                    }
                    break;
                }

                case 'installChromium': {
                    const result = await installChromium();
                    if (result.success) {
                        console.log(JSON.stringify({
                            binary: result.binary,
                            version: result.version,
                        }));
                    } else {
                        console.error(result.error);
                        process.exit(1);
                    }
                    break;
                }

                case 'installPuppeteerCore': {
                    const [npmPrefix] = commandArgs;
                    const result = await installPuppeteerCore({ npmPrefix: npmPrefix || undefined });
                    if (result.success) {
                        console.log(JSON.stringify({ path: result.path }));
                    } else {
                        console.error(result.error);
                        process.exit(1);
                    }
                    break;
                }

                case 'launchChromium': {
                    const [outputDir, extensionPathsJson] = commandArgs;
                    const extensionPaths = extensionPathsJson ? JSON.parse(extensionPathsJson) : [];
                    const result = await launchChromium({
                        outputDir: outputDir || 'chrome',
                        extensionPaths,
                    });
                    if (result.success) {
                        console.log(JSON.stringify({
                            cdpUrl: result.cdpUrl,
                            pid: result.pid,
                            port: result.port,
                        }));
                    } else {
                        console.error(result.error);
                        process.exit(1);
                    }
                    break;
                }

                case 'getCookiesViaCdp': {
                    const [portStr] = commandArgs;
                    const port = parseInt(portStr, 10);
                    if (isNaN(port) || port <= 0) {
                        console.error('Invalid port');
                        process.exit(1);
                    }
                    const cookies = await getCookiesViaCdp(port);
                    console.log(JSON.stringify(cookies));
                    break;
                }

                case 'getBrowserCdpUrl': {
                    const [
                        chromeSessionDir = '../chrome',
                        timeoutMsStr = '60000',
                        requireTargetIdStr = 'true',
                    ] = commandArgs;
                    const timeoutMs = parseInt(timeoutMsStr, 10);
                    if (isNaN(timeoutMs) || timeoutMs <= 0) {
                        console.error('Invalid timeoutMs');
                        process.exit(1);
                    }
                    const requireTargetId = !['0', 'false', 'no'].includes(
                        String(requireTargetIdStr).toLowerCase(),
                    );
                    const browserServerUrl = await getBrowserCdpUrl(chromeSessionDir, {
                        timeoutMs,
                        requireTargetId,
                    });
                    console.log(browserServerUrl);
                    break;
                }

                case 'killChrome': {
                    const [pidStr, outputDir] = commandArgs;
                    const pid = parseInt(pidStr, 10);
                    if (isNaN(pid)) {
                        console.error('Invalid PID');
                        process.exit(1);
                    }
                    await killChrome(pid, outputDir);
                    break;
                }

                case 'killZombieChrome': {
                    const [snapDir] = commandArgs;
                    const killed = await killZombieChrome(snapDir);
                    console.log(killed);
                    break;
                }

                case 'getExtensionId': {
                    const [unpacked_path] = commandArgs;
                    const id = getExtensionId(unpacked_path);
                    console.log(id);
                    break;
                }

                case 'loadExtensionManifest': {
                    const [unpacked_path] = commandArgs;
                    const manifest = loadExtensionManifest(unpacked_path);
                    console.log(JSON.stringify(manifest));
                    break;
                }

                case 'getExtensionLaunchArgs': {
                    const [extensions_json] = commandArgs;
                    const extensions = JSON.parse(extensions_json);
                    const launchArgs = getExtensionLaunchArgs(extensions);
                    console.log(JSON.stringify(launchArgs));
                    break;
                }

                case 'loadOrInstallExtension': {
                    const [webstore_id, name, extensions_dir] = commandArgs;
                    const ext = await loadOrInstallExtension({ webstore_id, name }, extensions_dir);
                    console.log(JSON.stringify(ext, null, 2));
                    break;
                }

                case 'readExtensionsMetadata': {
                    const [chromeSessionDir = '.', timeoutMsStr = '10000'] = commandArgs;
                    const timeoutMs = parseInt(timeoutMsStr, 10);
                    if (isNaN(timeoutMs) || timeoutMs <= 0) {
                        console.error('Invalid timeoutMs');
                        process.exit(1);
                    }
                    const deadline = Date.now() + timeoutMs;
                    let metadata = readExtensionsMetadata(chromeSessionDir);
                    while (metadata === null && Date.now() < deadline) {
                        await sleep(250);
                        metadata = readExtensionsMetadata(chromeSessionDir);
                    }
                    if (metadata === null) {
                        console.error(`Timeout waiting for extensions metadata in ${chromeSessionDir}`);
                        process.exit(1);
                    }
                    console.log(JSON.stringify(metadata));
                    break;
                }

                case 'getMachineType': {
                    console.log(getMachineType());
                    break;
                }

                case 'getLibDir': {
                    console.log(getLibDir());
                    break;
                }

                case 'getNodeModulesDir': {
                    console.log(getNodeModulesDir());
                    break;
                }

                case 'getExtensionsDir': {
                    console.log(getExtensionsDir());
                    break;
                }

                case 'getTestEnv': {
                    console.log(JSON.stringify(getTestEnv(), null, 2));
                    break;
                }

                case 'installExtensionWithCache': {
                    const [webstore_id, name, maybeNoCache] = commandArgs;
                    if (!webstore_id || !name) {
                        console.error('Usage: installExtensionWithCache <webstore_id> <name> [--no-cache]');
                        process.exit(1);
                    }
                    const ext = await installExtensionWithCache(
                        { webstore_id, name },
                        { noCache: maybeNoCache === '--no-cache' },
                    );
                    if (ext) {
                        console.log(JSON.stringify(ext, null, 2));
                    } else {
                        process.exit(1);
                    }
                    break;
                }

                default:
                    console.error(`Unknown command: ${command}`);
                    process.exit(1);
            }
        } catch (error) {
            console.error(`Error: ${error.message}`);
            process.exit(1);
        }
    })();
}
