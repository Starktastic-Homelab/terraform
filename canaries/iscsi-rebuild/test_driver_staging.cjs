// Runs unmodified v1.9.5 NodeStageVolume on private regular-file device surrogates.
// Only iSCSI discovery, block-device identity and mount/resize syscalls are mocked.
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const childProcess = require("node:child_process");

const root = __dirname;
const source = path.resolve(process.argv[2]);
const yaml = require(path.join(source, "node_modules/js-yaml"));
const { NodeManualDriver } = require(path.join(source, "src/driver/node-manual"));
const { Filesystem } = require(path.join(source, "src/utils/filesystem"));
const common = yaml.load(fs.readFileSync(path.join(root, "csi-values.yaml"), "utf8"));
const recovery = process.argv[3] ? yaml.load(fs.readFileSync(process.argv[3], "utf8")) : common;
const fixtures = process.argv[4] ? JSON.parse(fs.readFileSync(process.argv[4], "utf8")) : null;
const pv = fixtures?.items.find(item => item.kind === "PersistentVolume");
const volumeContext = pv?.spec.csi.volumeAttributes || {
  provisioner_driver: "node-manual", node_attach_driver: "iscsi",
  portal: "198.51.100.20:3260", iqn: "iqn.2026-09.invalid:staging-probe", lun: "0"
};
assert.equal(volumeContext.provisioner_driver, "node-manual");
const work = fs.mkdtempSync(path.join(root, ".checks/staging-"));
fs.chmodSync(work, 0o700);
const detection = common.node.driver.extraEnv.find(item => item.name === "FILESYSTEM_TYPE_DETECTION_STRATEGY")?.value;
assert.equal(detection, "blkid");
process.env.FILESYSTEM_TYPE_DETECTION_STRATEGY = detection;

function hash(file) {
  return crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex");
}

async function stage(label, options, seed) {
  const directory = path.join(work, label);
  fs.mkdirSync(directory, { mode: 0o700 });
  const image = path.join(directory, "device.img");
  const descriptor = fs.openSync(image, "wx", 0o600);
  fs.ftruncateSync(descriptor, 32 * 1024 * 1024);
  fs.closeSync(descriptor);
  if (seed) fs.copyFileSync(seed, image);
  const staging = path.join(directory, "staged");
  fs.mkdirSync(staging, { mode: 0o700 });
  const before = hash(image);
  const commands = [];
  let mounted = false;
  let mounts = 0;
  let resizes = 0;
  const filesystem = new Filesystem({
    executor: {
      spawn(command, args, spawnOptions) {
        assert(["blkid", "mkfs.ext4"].includes(command), `unexpected process ${command}`);
        assert.equal(args.at(-1), image);
        assert(fs.lstatSync(image).isFile() && !fs.lstatSync(image).isSymbolicLink());
        assert.equal(fs.statSync(image).size, 32 * 1024 * 1024);
        commands.push([command, ...args]);
        const child = childProcess.spawn(command, args, spawnOptions);
        child.stdin.end();
        return child;
      }
    }
  });
  filesystem.pathExists = async candidate => fs.existsSync(candidate);
  filesystem.realpath = async candidate => fs.realpathSync(candidate);
  filesystem.isBlockDevice = async candidate => candidate === image;
  filesystem.getBlockDevicePartitionCount = async () => 0;
  filesystem.getAllDeviceMapperSlaveDevices = async () => [];
  filesystem.expandFilesystem = async () => { resizes += 1; };
  const mount = {
    deviceIsMountedAtPath: async () => mounted,
    mount: async () => { mounted = true; mounts += 1; },
    umount: async () => { mounted = false; }
  };
  const iscsi = {
    iscsiadm: {
      createNodeDBEntry: async () => {},
      login: async () => {},
      getSession: async () => ({ portal: "198.51.100.20:3260" }),
      rescanSession: async () => {}
    },
    parsePortal: () => ({ host: "198.51.100.20", port: 3260 }),
    devicePathByPortalIQNLUN: async () => image
  };
  const logger = Object.fromEntries(
    ["error", "warn", "info", "verbose", "debug", "silly"].map(name => [name, () => {}])
  );
  const driver = new NodeManualDriver({ csiVersion: "1.5.0", logger }, structuredClone(options));
  driver.getDefaultFilesystemInstance = () => filesystem;
  driver.getDefaultMountInstance = () => mount;
  driver.getDefaultISCSIInstance = () => iscsi;
  driver.getDefaultNVMEoFInstance = () => ({});
  let error = null;
  try {
    await driver.NodeStageVolume({
      request: {
        volume_id: "iscsi-rebuild-canary-staging-probe",
        staging_target_path: staging,
        volume_capability: {
          access_type: "mount",
          access_mode: { mode: "SINGLE_NODE_SINGLE_WRITER" },
          mount: { fs_type: "ext4", mount_flags: [] }
        },
        volume_context: volumeContext,
        secrets: {}
      }
    });
  } catch (failure) {
    error = failure;
  }
  return { image, before, after: hash(image), error, commands, mounts, resizes };
}

(async () => {
  try {
    const initial = await stage("initial-control", common.driver.config);
    assert.equal(initial.error, null, "positive control must execute real driver staging");
    assert.notEqual(initial.after, initial.before, "positive control must really format the private image");
    assert.equal(initial.mounts, 1);
    const empty = await stage("retained-empty", recovery.driver.config);
    assert(empty.commands.some(argv => argv[0] === "mkfs.ext4"), "exercise the actual unknown-device branch");
    assert(empty.commands.find(argv => argv[0] === "mkfs.ext4").includes("-n"), "native no-create flag must reach mkfs");
    assert.equal(empty.after, empty.before, "recovery NodeStage must not initialize an unformatted retained device");
    assert(empty.error, "unformatted retained device must be refused before publish");
    assert.equal(empty.mounts, 0);
    assert.equal(empty.resizes, 0);
    const retained = await stage("retained-ext4", recovery.driver.config, initial.image);
    assert.equal(retained.error, null, "existing ext4 must still stage");
    assert.equal(retained.after, retained.before);
    assert(!retained.commands.some(argv => argv[0] === "mkfs.ext4"));
    console.log("PASS: actual pinned NodeStage refuses empty retained storage without writes; existing ext4 stages");
  } finally {
    fs.rmSync(work, { recursive: true, force: false });
  }
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
