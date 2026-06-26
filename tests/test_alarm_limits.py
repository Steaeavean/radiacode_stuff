"""Unit tests for alarm limit decode (RC-101 fixture, protocol §11.5)."""

from radiacode.radiacode import alarm_limits_from_raw_registers

# Raw batch order: CR_L1, CR_L2, DR_L1, DR_L2, DS_L1, DS_L2, DS_UNITS, CR_UNITS
RC101_RAW = [300, 600, 35, 60, 25000, 30000, 1, 0]


def test_alarm_limits_rc101_sv_mode():
    al = alarm_limits_from_raw_registers(RC101_RAW)
    assert al.dose_unit == 'Sv'
    assert al.count_unit == 'cps'
    assert al.l1_dose_rate == 0.35
    assert al.l2_dose_rate == 0.6
    assert al.l1_dose == 0.00025
    assert al.l2_dose == 0.0003
    assert al.l1_count_rate == 30.0
    assert al.l2_count_rate == 60.0


def test_alarm_limits_ds_units_low_byte_only():
    raw = RC101_RAW.copy()
    raw[6] = 0x01000000  # high byte set; low byte still 0 -> R mode
    al = alarm_limits_from_raw_registers(raw)
    assert al.dose_unit == 'R'
    assert al.l1_dose_rate == 35.0
    assert al.l1_dose == 0.025


def test_alarm_limits_cr_units_cpm():
    raw = RC101_RAW.copy()
    raw[7] = 1
    al = alarm_limits_from_raw_registers(raw)
    assert al.count_unit == 'cpm'
    assert al.l1_count_rate == 1800.0  # 300 cp/10s -> 30 cps * 60
    assert al.l2_count_rate == 3600.0
