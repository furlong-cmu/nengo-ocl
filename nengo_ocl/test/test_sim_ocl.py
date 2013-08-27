import pyopencl as cl

from nengo.tests.helpers import simulator_test_cases

from nengo_ocl import sim_ocl

# TODO:
# If PYOPENCL_CTX is not set, loop over all available devices
ctx = cl.create_some_context()

# -- Black-box testing:
#    Run each of the nengo simulator TestCase objects
#    using sim_ocl.Simulator.
for TestCase in simulator_test_cases:
    class MyTestCase(TestCase):
        simulator_test_case_ignore = True
        def Simulator(self, model):
            rval = sim_ocl.Simulator(ctx, model)
            rval.alloc_all()
            rval.plan_all()
            return rval
    MyTestCase.__name__ = TestCase.__name__
    globals()[TestCase.__name__] = MyTestCase
    # -- delete these symbols so that nose will not
    #    detect and run them as extra unit tests.
    del MyTestCase
    del TestCase


