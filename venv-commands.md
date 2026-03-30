# Running on Windows.

## Virtual Environments

**1. Delete the existing virtual environment**
```bash
rmdir /s /q xena_env
```

**2. Create the virtual environment for your scripts (`xoa_*`)**
```bash
py -3.12 -m venv xena_env
xena_env\Scripts\activate
pip install xoa-driver
deactivate
```

**3. Create the virtual environment for the Xena script library (`tdl_xoa_*`)**
```bash
py -3.12 -m venv tdl_xoa_env
tdl_xoa_env\Scripts\activate
cd tdl-xoa-python-script-library-main\tdl-xoa-python-script-library-main\rfc2544
pip install -r requirements.txt
deactivate
```

**4. Verify both environments look clean**
```bash
xena_env\Scripts\activate
pip list
deactivate

tdl_xoa_env\Scripts\activate
pip list
deactivate
```

---

## To use each one going forward:

For your scripts (`xena_packet_sweep.py`, `xena_rfc2544.py`, etc.):
```bash
xena_env\Scripts\activate
python xena_packet_sweep.py 30 0/0 0/1
```

For the Xena script library (`run_xoa_rfc.py`):
```bash
tdl_xoa_env\Scripts\activate
cd tdl-xoa-python-script-library-main\tdl-xoa-python-script-library-main\rfc2544
python run_xoa_rfc.py
```

> **Tip:** Run all these commands from the same parent folder where `xena_env` currently lives — that way the new environments are created in the same place and are easy to find.