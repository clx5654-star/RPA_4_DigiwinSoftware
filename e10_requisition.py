# -*- coding: utf-8 -*-
"""Legacy CLI compatibility wrapper for the purchase-requisition workflow.

New code must import and execute :mod:`e10_purchase_requisition`.  This file
contains no ERP business implementation and exists only so established local
commands do not fail immediately after the module rename.
"""

from e10_purchase_requisition import main


if __name__ == "__main__":
    raise SystemExit(main())
