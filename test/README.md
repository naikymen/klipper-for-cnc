# Klipper Test Suite

This directory contains the Klipper test suite, which includes both regular regression tests and batch mode testing.

## Running Tests

### Regular Regression Tests

To run the regular regression tests, you'll need:

1. The appropriate microcontroller dictionary files (e.g., `atmega2560.dict`)
2. A test configuration file (e.g., `k4cnc.cfg`)
3. A test case file (e.g., `k4cnc.test`)

### Microcontroller Dictionary Files

The microcontroller dictionary files are required for regular tests.

You can download them from: https://github.com/Klipper3d/klipper/issues/1438

### Batch Mode Testing (No Printer Required)

To run tests in batch mode, which simulates the printer's behavior without needing a physical connection:

1. Generate the microcontroller data dictionary (unless [already downloaded](#microcontroller-dictionary-files)):

```bash
make menuconfig  # Choose the correct microcontroller (i.e. atmega2560, atmega644p)
make  # Compile the microcontroller code, generating the dictionary file.
```

2. Run the tests:

```bash
rm _test*
clear
python scripts/test_klippy.py test/klippy/k4cnc.test -d test/dicts
rm _test*
```

> **NOTE**: It is really important to remove the test output files before running the tests,
> because the script appends the output to the files, and does not clear them when the test
> fails (which is usually the case when working...).

3. Translate the output to readable text (optional):

```bash
python ../klippy/parsedump.py atmega2560.dict _test_output > test.txt
```

## Additional Options

When running tests with `test_klippy.py`, you can use these additional options:

- `-v`: Verbose output
- `-k`: Keep temporary files for debugging
- `-d`: Specify dictionary directory

## Test File Structure

- `*.cfg`: Configuration files for the tests
- `*.test`: Test case files containing G-code commands and test scenarios
- `*.dict`: Microcontroller dictionary files (required for regular tests)

## Test Format

Test files (`.test`) follow this format:

```
# Comments start with #
DICTIONARY atmega2560.dict  # Required for regular tests
CONFIG k4cnc.cfg

# Test cases follow
G1 X1 Y1 Z1  # G-code commands
```

## Command-line Options for klippy.py

Here are the available command-line options for running klippy.py:

- `-i`, `--debuginput <file>`: Read commands from a file instead of from tty port
- `-I`, `--input-tty <path>`: Input tty name (default is `/tmp/printer`)
- `-a`, `--api-server <path>`: API server unix domain socket filename
- `-l`, `--logfile <path>`: Write log to file instead of stderr
- `-v`: Enable debug messages
- `-o`, `--debugoutput <file>`: Write output to file instead of to serial port
- `-d`, `--dictionary <file>`: File to read for mcu protocol dictionary
- `--import-test`: Perform an import module test

Example usage for batch mode testing:

```bash
python ../klippy/klippy.py test/klippy/k4cnc.test -o test.serial -v -d atmega2560.dict
```

The `-v` option is particularly useful for debugging as it enables debug messages, providing more detailed output during test execution.

## Dependencies

```bash
python3 -m venv klippy-env && \
source klippy-env/bin/activate && \
cd klipper && \
pip install -r scripts/klippy-requirements.txt
```

If the above fails, you can try installing the "latest" versions of each package.
There may be a compatibility issue with greenlet version 2.

```bash
# pip install cffi pyserial greenlet Jinja2 python-can markupsafe
pip install cffi pyserial greenlet
```
