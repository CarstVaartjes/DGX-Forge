#![forbid(unsafe_code)]

use std::{cell::RefCell, fs, time::Duration};

use tempfile::tempdir;
use vonk_agent::{
    inventory::{InventoryCollector, available_memory_bytes},
    process::{ProcessError, ProcessOutput, ProcessRunner, Program},
};

struct FakeRunner {
    calls: RefCell<Vec<(Program, Vec<String>)>>,
}

impl ProcessRunner for FakeRunner {
    fn run(
        &self,
        program: Program,
        arguments: &[String],
        _timeout: Duration,
    ) -> Result<ProcessOutput, ProcessError> {
        self.calls.borrow_mut().push((program, arguments.to_vec()));
        let stdout = match program {
            Program::NvidiaSmi => b"119808, 110000, 590.44\n".to_vec(),
            Program::Docker => b"28.3.3\n".to_vec(),
            _ => unreachable!(),
        };
        Ok(ProcessOutput {
            success: true,
            stdout,
            stderr: vec![],
        })
    }
}

#[test]
fn inventory_reports_physical_and_available_memory_disk_and_gpu() {
    let directory = tempdir().unwrap();
    let meminfo = directory.path().join("meminfo");
    fs::write(
        &meminfo,
        "MemTotal:       123456 kB\nMemAvailable:    65432 kB\n",
    )
    .unwrap();
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
    };
    let inventory = InventoryCollector {
        runner: &runner,
        meminfo_path: &meminfo,
        store_path: directory.path(),
        fabric_address: Some("192.168.100.2".parse().unwrap()),
        fabric_bandwidth_mbps: Some(200_000),
    }
    .collect()
    .unwrap();

    assert_eq!(inventory.memory_total_bytes, 123456 * 1024);
    assert_eq!(inventory.memory_available_bytes, 65432 * 1024);
    assert!(inventory.disk_total_bytes >= inventory.disk_available_bytes);
    assert_eq!(inventory.gpu_count, 1);
    assert_eq!(inventory.gpu_memory_total_bytes, 119808 * 1024 * 1024);
    assert_eq!(inventory.gpu_memory_free_bytes, 110000 * 1024 * 1024);
    assert_eq!(
        inventory.fabric_address.unwrap().to_string(),
        "192.168.100.2"
    );
    assert!(
        inventory
            .capabilities
            .contains(&"runtime.vonk.v1".to_owned())
    );
    assert!(
        inventory
            .capabilities
            .contains(&"fabric.tcp.mbps.200000".to_owned())
    );
    assert_eq!(runner.calls.borrow().len(), 2);
    assert_eq!(
        available_memory_bytes(&runner, &meminfo).unwrap(),
        65432 * 1024
    );
}

#[test]
fn malformed_or_inconsistent_memory_evidence_fails_closed() {
    let directory = tempdir().unwrap();
    let meminfo = directory.path().join("meminfo");
    fs::write(&meminfo, "MemTotal: 1 kB\nMemAvailable: 2 kB\n").unwrap();
    let runner = FakeRunner {
        calls: RefCell::new(vec![]),
    };
    assert!(
        InventoryCollector {
            runner: &runner,
            meminfo_path: &meminfo,
            store_path: directory.path(),
            fabric_address: None,
            fabric_bandwidth_mbps: None,
        }
        .collect()
        .is_err()
    );
    assert!(runner.calls.borrow().is_empty());
}
