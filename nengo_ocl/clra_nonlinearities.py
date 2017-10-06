from __future__ import division

from collections import OrderedDict

import numpy as np
import pyopencl as cl
from mako.template import Template
import nengo.dists as nengod
from nengo.utils.compat import is_number, itervalues, range

from nengo_ocl.raggedarray import RaggedArray
from nengo_ocl.clraggedarray import CLRaggedArray, to_device
from nengo_ocl.plan import Plan
from nengo_ocl.utils import as_ascii, indent, round_up


def get_mwgs(queue, cap=256):
    return min(queue.device.max_work_group_size, cap)


def update_param(textconf, raname, param, pname, index='n'):
    textconf[raname + pname] = (
        str(param[0]) if (param == param[0]).all() else
        '%s%ss[%s]' % (raname, pname, index))


def update_geometry(textconf, ra, raname, index='n'):
    update_param(textconf, raname, ra.shape0s, 'shape0', index=index)
    update_param(textconf, raname, ra.shape1s, 'shape1', index=index)
    update_param(textconf, raname, ra.stride0s, 'stride0', index=index)
    update_param(textconf, raname, ra.stride1s, 'stride1', index=index)
    textconf[raname + 'type'] = ra.ctype


def blockify_ij(max_size, ra):
    """Blockify a single matrix or vector using the offset method"""
    sizes = []
    inds = []
    offsets = []
    for k in range(len(ra)):
        size = ra.sizes[k]
        for offset in range(0, size, max_size):
            inds.append(k)
            sizes.append(min(size - offset, max_size))
            offsets.append(offset)

    return (np.array(sizes, dtype=np.int32),
            np.array(inds, dtype=np.int32),
            np.array(offsets, dtype=np.int32))


def blockify_matrices(max_size, ras):
    # NOTE: must be contiguous
    ras = list(ras)
    ra0 = ras[0]
    N = len(ra0)
    for ra in ras:
        assert len(ra) == N
        assert (ra.shape1s == ra0.shape1s).all()
        assert (ra.shape0s == ra0.shape0s).all()
        assert ((ra.shape1s == 1) | (ra.shape0s == ra.stride1s)).all(), "not contiguous"

    sizes = []
    inds = []
    starts = [[] for _ in ras]
    for i in range(N):
        size = ra0.sizes[i]
        startsi = [ra.starts[i] for ra in ras]
        while size > 0:
            sizes.append(min(size, max_size))
            size -= max_size
            inds.append(i)
            for k, ra in enumerate(ras):
                starts[k].append(startsi[k])
                startsi[k] += max_size

    return (np.array(sizes, dtype=np.int32),
            np.array(inds, dtype=np.int32),
            np.array(starts, dtype=np.int32))


def blockify_matrix(max_size, ra):
    sizes, inds, starts = blockify_matrices(max_size, [ra])
    return sizes, inds, starts[0]


def blockify_vectors(max_size, ras):
    ras = list(ras)
    ra0 = ras[0] if len(ras) > 0 else None
    N = len(ra0) if ra0 is not None else 0
    for ra in ras:
        assert len(ra) == N
        assert (ra.shape1s == 1).all()
        assert (ra.shape0s == ra0.shape0s).all()

    sizes = []
    inds = []
    starts = [[] for _ in ras]
    for i in range(N):
        size = ra0.shape0s[i]
        startsi = [ra.starts[i] for ra in ras]
        while size > 0:
            sizes.append(min(size, max_size))
            size -= max_size
            inds.append(i)
            for k, ra in enumerate(ras):
                starts[k].append(startsi[k])
                startsi[k] += max_size * ra.stride0s[i]

    return (np.array(sizes, dtype=np.int32),
            np.array(inds, dtype=np.int32),
            np.array(starts, dtype=np.int32))


def blockify_vector(max_size, ra):
    sizes, inds, starts = blockify_vectors(max_size, [ra])
    return sizes, inds, starts[0]


def plan_timeupdate(queue, step, time, dt):
    assert len(step) == len(time) == 1
    assert step.ctype == time.ctype == 'float'
    assert step.shape0s[0] == step.shape1s[0] == 1
    assert time.shape0s[0] == time.shape1s[0] == 1

    text = """
        ////////// MAIN FUNCTION //////////
        __kernel void timeupdate(
            __global const int *step_starts,
            __global float *step_data,
            __global const int *time_starts,
            __global float *time_data
        )
        {
            __global float *step = step_data + step_starts[0];
            __global float *time = time_data + time_starts[0];
            step[0] += 1;
            time[0] = ${dt} * step[0];
        }
        """

    text = as_ascii(Template(text, output_encoding='ascii').render(dt=dt))
    full_args = (step.cl_starts, step.cl_buf, time.cl_starts, time.cl_buf)
    _fn = cl.Program(queue.context, text).build().timeupdate
    _fn.set_args(*[arr.data for arr in full_args])

    gsize = (1,)
    lsize = None
    plan = Plan(queue, _fn, gsize, lsize=lsize, name="cl_timeupdate")
    plan.full_args = full_args     # prevent garbage-collection
    return plan


def plan_reset(queue, Y, values, tag=None):
    assert len(Y) == len(values)

    assert (Y.stride0s == 1).all()
    assert ((Y.shape1s == 1) | (Y.stride1s == Y.shape0s)).all()
    assert Y.ctype == values.ctype

    text = """
        ////////// MAIN FUNCTION //////////
        __kernel void reset(
            __global const ${Ytype} *values,
            __global const int *Ysizes,
            __global const int *Ystarts,
            __global ${Ytype} *Ydata
        )
        {
            const int i = get_global_id(0);
            int n = get_global_id(1);

    % for k in range(n_per_item):
            if (n < ${N} && i < Ysizes[n])
                (Ydata + Ystarts[n])[i] = values[n];
            n += get_global_size(1);
    % endfor
        }
        """

    n_per_item = 1
    lsize0 = 16
    lsize1 = get_mwgs(queue, cap=256) // lsize0
    # lsize0 = min(256, Y.sizes.max())

    Ysizes, Yinds, Ystarts = blockify_matrix(lsize0, Y)
    clYsizes = to_device(queue, Ysizes)
    clYstarts = to_device(queue, Ystarts)
    values = values.get()
    clvalues = to_device(queue, values[Yinds])

    N = len(Ysizes)
    NN = -(-N // n_per_item)  # ceiling division
    lsize = (lsize0, lsize1)
    gsize = (lsize0, round_up(NN, lsize1))
    # lsize = None
    # gsize = (lsize0, NN)

    textconf = dict(Ytype=Y.ctype, N=N, n_per_item=n_per_item)
    text = as_ascii(Template(text, output_encoding='ascii').render(**textconf))

    full_args = (
        clvalues,
        clYsizes,
        clYstarts,
        Y.cl_buf,
    )
    _fn = cl.Program(queue.context, text).build().reset
    _fn.set_args(*[arr.data for arr in full_args])

    plan = Plan(queue, _fn, gsize, lsize=lsize, name="cl_reset", tag=tag)
    plan.full_args = full_args     # prevent garbage-collection
    plan.bw_per_call = (
        Y.nbytes + values.nbytes + clYsizes.nbytes + clYstarts.nbytes)
    plan.description = (
        "groups: %d; items: %d; items/group: %0.1f [%d, %d]" %
        (len(Y), Y.sizes.sum(), Y.sizes.mean(), Y.sizes.min(), Y.sizes.max()))
    return plan


def plan_copy(queue, X, Y, incs, tag=None):
    assert len(X) == len(Y)
    assert (X.shape0s == Y.shape0s).all()
    assert (X.shape1s == Y.shape1s).all()
    assert X.ctype == Y.ctype

    text = """
        ////////// MAIN FUNCTION //////////
        __kernel void copy(
    % if inc is None:
            __global const int *incdata,
    % endif
            __global const int *offsets,
            __global const int *shape0s,
            __global const int *shape1s,
            __global const int *Xstride0s,
            __global const int *Xstride1s,
            __global const int *Xstarts,
            __global const ${Xtype} *Xdata,
            __global const int *Ystride0s,
            __global const int *Ystride1s,
            __global const int *Ystarts,
            __global ${Ytype} *Ydata
        )
        {
            const int n = get_global_id(1);
            const int ij = get_global_id(0) + offsets[n];

            const int shape0 = shape0s[n];
            const int i = ij % shape0;
            const int j = ij / shape0;
            const int xo = Xstarts[n] + i*${Xstride0} + j*${Xstride1};
            const int yo = Ystarts[n] + i*${Ystride0} + j*${Ystride1};

            if (j < shape1s[n]) {
    % if inc is True:
                Ydata[yo] += Xdata[xo];
    % elif inc is False:
                Ydata[yo]  = Xdata[xo];
    % else:
                if (incdata[n])  Ydata[yo] += Xdata[xo];
                else             Ydata[yo]  = Xdata[xo];
    % endif
            }
        }
        """

    lsize0 = get_mwgs(queue, cap=256)
    sizes, inds, offsets = blockify_ij(lsize0, Y)

    lsize = None
    gsize = (lsize0, len(sizes))

    textconf = dict(inc=None)
    update_geometry(textconf, X, 'X', index='n')
    update_geometry(textconf, Y, 'Y', index='n')

    full_args = [
        to_device(queue, offsets),
        to_device(queue, X.shape0s[inds]),
        to_device(queue, X.shape1s[inds]),
        to_device(queue, X.stride0s[inds]),
        to_device(queue, X.stride1s[inds]),
        to_device(queue, X.starts[inds]),
        X.cl_buf,
        to_device(queue, Y.stride0s[inds]),
        to_device(queue, Y.stride1s[inds]),
        to_device(queue, Y.starts[inds]),
        Y.cl_buf,
    ]
    if (incs == 0).all():
        textconf['inc'] = False
    elif (incs == 1).all():
        textconf['inc'] = True
    else:
        full_args.insert(0, to_device(queue, incs[inds].astype(np.int32)))

    text = as_ascii(Template(text, output_encoding='ascii').render(**textconf))
    _fn = cl.Program(queue.context, text).build().copy
    _fn.set_args(*[arr.data for arr in full_args])

    plan = Plan(queue, _fn, gsize, lsize=lsize, name="cl_copy", tag=tag)
    plan.full_args = tuple(full_args)  # prevent garbage-collection
    plan.bw_per_call = X.nbytes + Y.nbytes
    plan.description = (
        "groups: %d; items: %d; items/group: %0.1f [%d, %d]" %
        (len(X), X.sizes.sum(), X.sizes.mean(), X.sizes.min(), X.sizes.max()))
    return plan


def plan_slicedcopy(queue, X, Y, Xinds, Yinds, incs, tag=None):
    N = len(X)
    assert len(X) == len(Y) == len(Xinds) == len(Yinds)

    for arr in (X, Y, Xinds, Yinds):
        assert (arr.shape1s == 1).all()
    for arr in (Xinds, Yinds):
        assert (arr.stride0s == 1).all()
    assert (Xinds.shape0s == Yinds.shape0s).all()

    assert X.ctype == Y.ctype
    assert Xinds.ctype == Yinds.ctype == 'int'

    text = """
        ////////// MAIN FUNCTION //////////
        __kernel void slicedcopy(
    % if inc is None:
            __global const int *incdata,
    % endif
            __global const int *Xstride0s,
            __global const int *Xstarts,
            __global const ${Xtype} *Xdata,
            __global const int *Ystride0s,
            __global const int *Ystarts,
            __global ${Ytype} *Ydata,
            __global const int *Isizes,
            __global const int *XIstarts,
            __global const int *XIdata,
            __global const int *YIstarts,
            __global const int *YIdata
        )
        {
            const int i = get_global_id(0);
            const int n = get_global_id(1);
            if (n >= ${N})
                return;

            __global const ${Xtype} *a = Xdata + Xstarts[n];
            __global ${Ytype} *b = Ydata + Ystarts[n];
            __global const int *aind = XIdata + XIstarts[n];
            __global const int *bind = YIdata + YIstarts[n];

            if (i < Isizes[n]) {
    % if inc is True:
                b[bind[i]*${Ystride0}] += a[aind[i]*${Xstride0}];
    % elif inc is False:
                b[bind[i]*${Ystride0}] = a[aind[i]*${Xstride0}];
    % else:
                if (incdata[n])
                    b[bind[i]*${Ystride0}] += a[aind[i]*${Xstride0}];
                else
                    b[bind[i]*${Ystride0}] = a[aind[i]*${Xstride0}];
    % endif
            }
        }
        """

    lsize0 = 16
    lsize1 = get_mwgs(queue) // lsize0

    sizes, inds, [XIstarts, YIstarts] = blockify_vectors(
        lsize0, [Xinds, Yinds])

    N = len(sizes)
    lsize = (lsize0, lsize1)
    gsize = (lsize0, round_up(N, lsize1))

    textconf = dict(N=N, inc=None)
    update_geometry(textconf, X, 'X', index='n')
    update_geometry(textconf, Y, 'Y', index='n')

    full_args = [
        to_device(queue, X.stride0s[inds]),
        to_device(queue, X.starts[inds]),
        X.cl_buf,
        to_device(queue, Y.stride0s[inds]),
        to_device(queue, Y.starts[inds]),
        Y.cl_buf,
        to_device(queue, sizes),
        to_device(queue, XIstarts),
        Xinds.cl_buf,
        to_device(queue, YIstarts),
        Yinds.cl_buf,
    ]
    if (incs == 0).all():
        textconf['inc'] = False
    elif (incs == 1).all():
        textconf['inc'] = True
    else:
        full_args.insert(0, to_device(queue, incs[inds].astype(np.int32)))

    text = as_ascii(Template(text, output_encoding='ascii').render(**textconf))
    _fn = cl.Program(queue.context, text).build().slicedcopy
    _fn.set_args(*[arr.data for arr in full_args])

    plan = Plan(queue, _fn, gsize, lsize=lsize, name="cl_slicedcopy", tag=tag)
    plan.full_args = tuple(full_args)  # prevent garbage-collection
    plan.bw_per_call = 2 * (Xinds.nbytes + Xinds.sizes.sum()*X.dtype.itemsize)
    plan.description = (
        "groups: %d; items: %d; items/group: %0.1f [%d, %d]" %
        (len(Xinds), Xinds.sizes.sum(),
         Xinds.sizes.mean(), Xinds.sizes.min(), Xinds.sizes.max()))
    return plan


def plan_elementwise_inc(queue, A, X, Y, tag=None):
    """Implements an element-wise increment Y += A * X"""
    assert len(Y) == len(X) == len(A)

    assert ((X.shape0s == 1) | (X.shape0s == Y.shape0s)).all()
    assert ((X.shape1s == 1) | (X.shape1s == Y.shape1s)).all()
    assert ((A.shape0s == 1) | (A.shape0s == Y.shape0s)).all()
    assert ((A.shape1s == 1) | (A.shape1s == Y.shape1s)).all()

    assert A.ctype == X.ctype == Y.ctype

    text = """
        inline ${Ytype} get_element(
            __global const ${Ytype} *data,
            const int shape0, const int shape1,
            const int stride0, const int stride1,
            const int i, const int j
        )
        {
            if (shape0 == 1 && shape1 == 1)
                return data[0];
            else if (shape0 == 1)
                return data[j*stride1];
            else if (shape1 == 1)
                return data[i*stride0];
            else
                return data[i*stride0 + j*stride1];
        }

        ////////// MAIN FUNCTION //////////
        __kernel void elementwise_inc(
            __global const int *offsets,
            __global const int *Ashape0s,
            __global const int *Ashape1s,
            __global const int *Astride0s,
            __global const int *Astride1s,
            __global const int *Astarts,
            __global const ${Atype} *Adata,
            __global const int *Xshape0s,
            __global const int *Xshape1s,
            __global const int *Xstride0s,
            __global const int *Xstride1s,
            __global const int *Xstarts,
            __global const ${Xtype} *Xdata,
            __global const int *Yshape0s,
            __global const int *Yshape1s,
            __global const int *Ystride0s,
            __global const int *Ystride1s,
            __global const int *Ystarts,
            __global ${Ytype} *Ydata
        )
        {
            const int n = get_global_id(1);
            const int ij = get_global_id(0) + offsets[n];

            __global const ${Atype} *a = Adata + Astarts[n];
            __global const ${Xtype} *x = Xdata + Xstarts[n];
            __global ${Ytype} *y = Ydata + Ystarts[n];

            const int Yshape0 = ${Yshape0};
            const int i = ij % Yshape0;
            const int j = ij / Yshape0;

            ${Atype} aa = get_element(
                a, ${Ashape0}, ${Ashape1}, ${Astride0}, ${Astride1}, i, j);
            ${Xtype} xx = get_element(
                x, ${Xshape0}, ${Xshape1}, ${Xstride0}, ${Xstride1}, i, j);

            if (j < ${Yshape1})
                y[i*${Ystride0} + j*${Ystride1}] += aa * xx;
        }
        """

    # --- blockify
    lsize0 = get_mwgs(queue, cap=256)
    sizes, inds, offsets = blockify_ij(lsize0, Y)

    textconf = dict()
    update_geometry(textconf, A, 'A', index='n')
    update_geometry(textconf, X, 'X', index='n')
    update_geometry(textconf, Y, 'Y', index='n')
    text = as_ascii(Template(text, output_encoding='ascii').render(**textconf))

    full_args = (
        to_device(queue, offsets),
        to_device(queue, A.shape0s[inds]),
        to_device(queue, A.shape1s[inds]),
        to_device(queue, A.stride0s[inds]),
        to_device(queue, A.stride1s[inds]),
        to_device(queue, A.starts[inds]),
        A.cl_buf,
        to_device(queue, X.shape0s[inds]),
        to_device(queue, X.shape1s[inds]),
        to_device(queue, X.stride0s[inds]),
        to_device(queue, X.stride1s[inds]),
        to_device(queue, X.starts[inds]),
        X.cl_buf,
        to_device(queue, Y.shape0s[inds]),
        to_device(queue, Y.shape1s[inds]),
        to_device(queue, Y.stride0s[inds]),
        to_device(queue, Y.stride1s[inds]),
        to_device(queue, Y.starts[inds]),
        Y.cl_buf,
    )
    _fn = cl.Program(queue.context, text).build().elementwise_inc
    _fn.set_args(*[arr.data for arr in full_args])

    gsize = (lsize0, len(sizes))
    plan = Plan(
        queue, _fn, gsize, lsize=None, name="cl_elementwise_inc", tag=tag)
    plan.full_args = full_args     # prevent garbage-collection
    plan.flops_per_call = 2 * Y.sizes.sum()
    plan.bw_per_call = A.nbytes + X.nbytes + Y.nbytes
    plan.description = (
        "groups: %d; items: %d; items/group: %0.1f [%d, %d]" %
        (len(Y), Y.sizes.sum(), Y.sizes.mean(), Y.sizes.min(), Y.sizes.max()))
    return plan


def plan_linearfilter(queue, X, Y, A, B, Xbuf, Ybuf, tag=None):
    """
    Implements a filter of the form

        y[n+1] + a[0] y[n] + ... + a[i] y[n-i] = b[0] x[n] + ... + b[j] x[n-j]
    """
    assert len(X) == len(Y) == len(A) == len(B) == len(Xbuf) == len(Ybuf)

    assert (X.shape0s == Y.shape0s).all()
    assert (X.shape1s == Y.shape1s).all()

    for arr in (Xbuf, Ybuf):  # contiguous
        assert (arr.shape0s == arr.stride1s).all()
        assert (arr.stride0s == 1).all()
    for arr in (A, B):  # vectors
        assert (arr.shape1s == 1).all()
        assert (arr.stride0s == 1).all()

    assert (B.shape0s >= 1).all()
    assert (Xbuf.shape1s == B.shape0s).all()
    assert (Ybuf.shape1s == A.shape0s).all()
    # assert ((B.shape0s == 1) | (Xbuf.shape1s == B.shape0s)).all()
    # assert ((A.shape0s == 1) | (Ybuf.shape1s == A.shape0s)).all()

    assert (Xbuf.shape0s == X.sizes).all()
    assert (Ybuf.shape0s == Y.sizes).all()
    assert X.ctype == Xbuf.ctype
    assert Y.ctype == Ybuf.ctype

    text = """
        ////////// MAIN FUNCTION //////////
        __kernel void linearfilter(
            __global const int *offsets,
            __global const int *shape0s,
            __global const int *shape1s,
            __global const int *Xstride0s,
            __global const int *Xstride1s,
            __global const int *Xstarts,
            __global const ${Xtype} *x,
            __global const int *Ystride0s,
            __global const int *Ystride1s,
            __global const int *Ystarts,
            __global ${Ytype} *y,
            __global const int *Ashape0s,
            __global const int *Astarts,
            __global const ${Atype} *Adata,
            __global const int *Bshape0s,
            __global const int *Bstarts,
            __global const ${Btype} *Bdata,
            __global const int *Xbufstarts,
            __global ${Xtype} *Xbufdata,
            __global const int *Ybufstarts,
            __global ${Ytype} *Ybufdata,
            __global const int *Xbufpos,
            __global const int *Ybufpos
        )
        {
            const int k = get_global_id(1);
            const int ij = get_global_id(0) + offsets[k];

            const int shape0 = shape0s[k];
            const int shape1 = shape1s[k];
            const int i = ij % shape0;
            const int j = ij / shape0;
            const int xij = Xstarts[k] + i*${Xstride0} + j*${Xstride1};
            const int yij = Ystarts[k] + i*${Ystride0} + j*${Ystride1};

            const int na = Ashape0s[k];
            const int nb = Bshape0s[k];

            __local ${Atype} a[${na_max}];
            __local ${Btype} b[${nb_max}];
            const int ti = get_local_id(0);
            if (ti < na)
                a[ti] = (Adata + Astarts[k])[ti];
            if (ti < nb)
                b[ti] = (Bdata + Bstarts[k])[ti];
            barrier(CLK_LOCAL_MEM_FENCE);

            if (j >= shape1)
                return;

            if (na == 0 && nb == 1) {
                y[yij] = b[0] * x[xij];
            } else if (na == 1 && nb == 1) {
                y[yij] = b[0] * x[xij] - a[0] * y[yij];
    % if uses_buf:  # save registers: only compile if needed
            } else {  // general filtering
                __global ${Xtype} *xbuf = Xbufdata + Xbufstarts[k];
                __global ${Ytype} *ybuf = Ybufdata + Ybufstarts[k];
                const int ix = Xbufpos[k];
                const int iy = Ybufpos[k];
                const int ix1 = (ix > 0) ? ix - 1 : nb - 1;
                const int iy1 = (iy > 0) ? iy - 1 : na - 1;
                const int size = shape0 * shape1;

                ${Ytype} yi = b[0] * x[xij];
                if (nb > 1) {
                    xbuf[ix*size + ij] = x[xij];  // copy input to buffer
                    for (int p = 1; p < nb; p++)
                        yi += b[p] * xbuf[((ix + p) % nb)*size + ij];
                }

                if (na > 0) {
                    yi -= a[0] * y[yij];
                    if (na > 1) {
                        for (int p = 1; p < na; p++)
                            yi -= a[p] * ybuf[((iy + p) % na)*size + ij];

                        ybuf[iy1*size + ij] = yi;  // copy output to buffer
                    }
                }

                y[yij] = yi;
    % endif
            }
        }

    % if uses_buf:  # only compile if needed
        __kernel void linearfilter_inc(
            __global const int *Ashape0s,
            __global const int *Bshape0s,
            __global int *Xbufpos,
            __global int *Ybufpos
        )
        {
            const int k = get_global_id(0);
            const int na = Ashape0s[k];
            const int nb = Bshape0s[k];

            const int ix = Xbufpos[k];
            const int iy = Ybufpos[k];
            Xbufpos[k] = (ix > 0) ? ix - 1 : nb - 1;
            Ybufpos[k] = (iy > 0) ? iy - 1 : na - 1;
        }
    % endif
        """

    na_max = A.sizes.max()
    nb_max = B.sizes.max()
    assert nb_max >= 1
    uses_buf = na_max > 1 or nb_max > 1

    textconf = dict(
        Atype=A.ctype, Btype=B.ctype,
        na_max=na_max, nb_max=nb_max, uses_buf=uses_buf,
    )
    update_geometry(textconf, X, 'X', index='k')
    update_geometry(textconf, Y, 'Y', index='k')
    text = as_ascii(Template(text, output_encoding='ascii').render(**textconf))

    max_len = X.sizes.max()
    lsize0 = min(max(max_len, na_max, nb_max), get_mwgs(queue))
    assert na_max <= lsize0 and nb_max <= lsize0

    sizes, inds, offsets = blockify_ij(lsize0, X)
    n = len(sizes)

    Xbufpos = to_device(queue, np.zeros(n if uses_buf else 0, dtype='int32'))
    Ybufpos = to_device(queue, np.zeros(n if uses_buf else 0, dtype='int32'))

    full_args = (
        to_device(queue, offsets),
        to_device(queue, X.shape0s[inds]), to_device(queue, X.shape1s[inds]),
        to_device(queue, X.stride0s[inds]), to_device(queue, X.stride1s[inds]),
        to_device(queue, X.starts[inds]), X.cl_buf,
        to_device(queue, Y.stride0s[inds]), to_device(queue, Y.stride1s[inds]),
        to_device(queue, Y.starts[inds]), Y.cl_buf,
        to_device(queue, A.shape0s[inds]), to_device(queue, A.starts[inds]),
        A.cl_buf,
        to_device(queue, B.shape0s[inds]), to_device(queue, B.starts[inds]),
        B.cl_buf,
        to_device(queue, Xbuf.starts[inds]), Xbuf.cl_buf,
        to_device(queue, Ybuf.starts[inds]), Ybuf.cl_buf,
        Xbufpos, Ybufpos,
    )

    # --- build and print info (change maxregcount to avoid cache, force build)
    # program = cl.Program(queue.context, text).build(
    #     options=['-cl-nv-maxrregcount=55', '-cl-nv-verbose'])
    # print(program.get_build_info(queue.device, cl.program_build_info.LOG))

    program = cl.Program(queue.context, text).build()
    _fn = program.linearfilter
    _fn.set_args(*[arr.data for arr in full_args])

    lsize = (lsize0, 1)
    gsize = (lsize0, n)
    plan = Plan(
        queue, _fn, gsize, lsize=lsize, name="cl_linearfilter", tag=tag)
    plan.full_args = full_args     # prevent garbage-collection
    plan.bw_per_call = (
        X.nbytes + Y.nbytes + A.nbytes + B.nbytes + Xbuf.nbytes + Ybuf.nbytes)
    plan.description = (
        "groups: %d; items: %d; items/group: %0.1f [%d, %d]" %
        (len(Y), Y.sizes.sum(), Y.sizes.mean(), Y.sizes.min(), Y.sizes.max()))

    if not uses_buf:
        return [plan]
    else:
        inc = program.linearfilter_inc
        inc_args = (
            to_device(queue, A.shape0s[inds]),
            to_device(queue, B.shape0s[inds]),
            Xbufpos, Ybufpos)
        inc.set_args(*[arr.data for arr in inc_args])
        inc_plan = Plan(queue, inc, gsize=(n,), name="cl_linearfilter_inc")
        inc_plan.full_args = inc_args     # prevent garbage-collection

        return [plan, inc_plan]


def plan_probes(queue, periods, X, Y, tag=None):
    """
    Parameters
    ----------
    P : raggedarray of ints
        The period (in time-steps) of each probe
    """
    assert len(X) == len(Y)
    assert len(X) == len(periods)
    assert X.ctype == Y.ctype
    N = len(X)

    # N.B.  X[i].shape = (M, N)
    #       Y[i].shape = (M * N, buflen)
    for arr in [X, Y]:
        assert (arr.stride0s == 1).all()
    assert (X.shape0s * X.shape1s == Y.shape0s).all()
    assert (Y.stride1s == Y.shape0s).all()

    periods = np.asarray(periods, dtype='float32')
    cl_periods = to_device(queue, periods)
    cl_countdowns = to_device(queue, periods - 1)
    cl_bufpositions = to_device(queue, np.zeros(N, dtype='int32'))

    text = """
        ////////// MAIN FUNCTION //////////
        __kernel void probes(
            __global ${Ctype} *countdowns,
            __global int *bufpositions,
            __global const ${Ptype} *periods,
            __global const int *Xstarts,
            __global const int *Xshape0s,
            __global const int *Xshape1s,
            __global const ${Xtype} *Xdata,
            __global const int *Ystarts,
            __global ${Ytype} *Ydata
        )
        {
            const int n = get_global_id(1);
            const ${Ctype} countdown = countdowns[n];

            if (countdown <= 0) {
                const int n_dims = Xshape0s[n] * Xshape1s[n];
                __global const ${Xtype} *x = Xdata + Xstarts[n];
                const int bufpos = bufpositions[n];

                __global ${Ytype} *y = Ydata + Ystarts[n] + bufpos * n_dims;

                for (int ii = get_global_id(0);
                         ii < n_dims;
                         ii += get_global_size(0))
                {
                    y[ii] = x[ii];
                }
                // This should *not* cause deadlock because
                // all local threads guaranteed to be
                // in this branch together.
                barrier(CLK_LOCAL_MEM_FENCE);
                if (get_global_id(0) == 0)
                {
                    countdowns[n] = countdown + periods[n] - 1;
                    bufpositions[n] = bufpos + 1;
                }
            }
            else
            {
                barrier(CLK_LOCAL_MEM_FENCE);
                if (get_global_id(0) == 0)
                {
                    countdowns[n] = countdown - 1;
                }
            }
        }
        """

    textconf = dict(N=N,
                    Xtype=X.ctype,
                    Ytype=Y.ctype,
                    Ctype=cl_countdowns.ctype,
                    Ptype=cl_periods.ctype)
    text = as_ascii(Template(text, output_encoding='ascii').render(**textconf))

    full_args = (
        cl_countdowns,
        cl_bufpositions,
        cl_periods,
        X.cl_starts,
        X.cl_shape0s,
        X.cl_shape1s,
        X.cl_buf,
        Y.cl_starts,
        Y.cl_buf,
    )
    _fn = cl.Program(queue.context, text).build().probes
    _fn.set_args(*[arr.data for arr in full_args])

    max_len = min(max(X.shape0s), get_mwgs(queue))
    gsize = (max_len, N,)
    lsize = (max_len, 1)
    plan = Plan(queue, _fn, gsize, lsize=lsize, name="cl_probes", tag=tag)
    plan.full_args = full_args     # prevent garbage-collection
    plan.cl_bufpositions = cl_bufpositions
    plan.Y = Y
    plan.bw_per_call = (2*X.nbytes + cl_periods.nbytes +
                        cl_countdowns.nbytes + cl_bufpositions.nbytes)
    plan.description = (
        "groups: %d; items: %d; items/group: %0.1f [%d, %d]" %
        (len(X), X.sizes.sum(), X.sizes.mean(), X.sizes.min(), X.sizes.max()))
    return plan


def plan_direct(queue, code, init, input_names, inputs, output, tag=None):
    from . import ast_conversion

    assert len(input_names) == len(inputs)

    N = len(inputs[0])
    for x in inputs:
        assert len(x) == len(output)
    for x in inputs + [output]:
        assert (x.shape1s == 1).all()  # vectors
        assert (x.stride0s == 1).all()  # contiguous

    input_types = [x.ctype for x in inputs]
    output_type = output.ctype

    text = """
        ////////// MAIN FUNCTION //////////
        __kernel void direct(
% for iname, itype in zip(input_names, input_types):
            __global const int *${iname}_starts__,
            __global const ${itype} *${iname}_data__,
% endfor
            __global const int *${oname}_starts__,
            __global ${otype} *${oname}_data__
        )
        {
            const int n = get_global_id(0);
            if (n >= ${N}) return;

% for iname, itype in zip(input_names, input_types):
            __global const ${itype} *${iname} =
                ${iname}_data__ + ${iname}_starts__[n];
% endfor
            __global ${otype} *${oname} =
                ${oname}_data__ + ${oname}_starts__[n];

            /////vvvvv USER DECLARATIONS BELOW vvvvv
${init}

            /////vvvvv USER COMPUTATIONS BELOW vvvvv
${code}
            // END OF FUNC: put nothing after user code, since it can return
        }
        """

    textconf = dict(init=indent(init, 12),
                    code=indent(code, 12),
                    N=N, input_names=input_names, input_types=input_types,
                    oname=ast_conversion.OUTPUT_NAME, otype=output_type,
                    )
    text = as_ascii(Template(text, output_encoding='ascii').render(**textconf))

    full_args = []
    for x in inputs:
        full_args.extend([x.cl_starts, x.cl_buf])
    full_args.extend([output.cl_starts, output.cl_buf])
    _fn = cl.Program(queue.context, text).build().direct
    _fn.set_args(*[arr.data for arr in full_args])

    gsize = (N,)
    plan = Plan(queue, _fn, gsize, lsize=None, name="cl_direct", tag=tag)
    plan.full_args = tuple(full_args)  # prevent garbage-collection
    plan.description = (
        "groups: %d; items: %d; items/group: %0.1f [%d, %d]" %
        (len(output), output.sizes.sum(),
         output.sizes.mean(), output.sizes.min(), output.sizes.max()))
    return plan


def plan_lif(queue, dt, J, V, W, outS, ref, tau, N=None, tau_n=None,
             inc_n=None, upsample=1, fastlif=False, **kwargs):
    adaptive = N is not None
    assert J.ctype == 'float'
    for array in [V, W, outS]:
        assert V.ctype == J.ctype

    inputs = dict(J=J, V=V, W=W)
    outputs = dict(outV=V, outW=W, outS=outS)
    parameters = dict(tau=tau, ref=ref)
    if adaptive:
        assert all(ary is not None for ary in [N, tau_n, inc_n])
        assert N.ctype == J.ctype
        inputs.update(dict(N=N))
        outputs.update(dict(outN=N))
        parameters.update(dict(tau_n=tau_n, inc_n=inc_n))

    dt = float(dt)
    textconf = dict(
        type=J.ctype, dt=dt, upsample=upsample, adaptive=adaptive,
        dtu=dt/upsample, dtu_inv=upsample/dt, dt_inv=1/dt, fastlif=fastlif)
    decs = """
        char spiked;
        ${type} dV;
        const ${type} V_threshold = 1;
        const ${type} dtu = ${dtu}, dtu_inv = ${dtu_inv}, dt_inv = ${dt_inv};
% if adaptive:
        const ${type} dt = ${dt};
% endif
%if fastlif:
        const ${type} delta_t = dtu;
%else:
        ${type} delta_t;
%endif
        """
    # TODO: could precompute -expm1(-dtu / tau)
    text = """
        spiked = 0;

% for ii in range(upsample):
        W -= dtu;
% if not fastlif:
        delta_t = (W > dtu) ? 0 : (W < 0) ? dtu : dtu - W;
% endif
% if adaptive:
        dV = -expm1(-delta_t / tau) * (J - N - V);
% else:
        dV = -expm1(-delta_t / tau) * (J - V);
% endif
        V += dV;

% if fastlif:
        if (V < 0 || W > dtu)
            V = 0;
        else if (W >= 0)
            V *= 1 - W * dtu_inv;
% endif

        if (V > V_threshold) {
% if fastlif:
            const ${type} overshoot = dtu * (V - V_threshold) / dV;
            W = ref - overshoot + dtu;
% else:
            const ${type} t_spike = dtu + tau * log1p(
                -(V - V_threshold) / (J - V_threshold));
            W = ref + t_spike;
% endif
            V = 0;
            spiked = 1;
        }
% if not fastlif:
         else if (V < 0) {
            V = 0;
        }
% endif

% endfor
        outV = V;
        outW = W;
        outS = (spiked) ? dt_inv : 0;
% if adaptive:
        outN = N + (dt / tau_n) * (inc_n * outS - N);
% endif
        """
    decs = as_ascii(Template(decs, output_encoding='ascii').render(**textconf))
    text = as_ascii(Template(text, output_encoding='ascii').render(**textconf))
    cl_name = "cl_alif" if adaptive else "cl_lif"
    return _plan_template(
        queue, cl_name, text, declares=decs,
        inputs=inputs, outputs=outputs, parameters=parameters, **kwargs)


def plan_lif_rate(queue, dt, J, R, ref, tau, N=None, tau_n=None, inc_n=None,
                  **kwargs):
    assert J.ctype == 'float'
    assert R.ctype == J.ctype
    adaptive = N is not None

    inputs = dict(J=J)
    outputs = dict(R=R)
    parameters = dict(tau=tau, ref=ref)
    textconf = dict(type=J.ctype, dt=dt, adaptive=adaptive)
    if adaptive:
        assert all(ary is not None for ary in [N, tau_n, inc_n])
        assert N.ctype == J.ctype
        inputs.update(dict(N=N))
        outputs.update(dict(outN=N))
        parameters.update(dict(tau_n=tau_n, inc_n=inc_n))

    decs = """
        const ${type} c0 = 0, c1 = 1;
        const ${type} dt = ${dt};
        """
    text = """
    % if adaptive:
        J = max(J - N - 1, c0);
    % else:
        J = max(J - 1, c0);
    % endif
        R = c1 / (ref + tau * log1p(c1/J));
    % if adaptive:
        outN = N + (dt / tau_n) * (inc_n*R - N);
    % endif
        """
    decs = as_ascii(Template(decs, output_encoding='ascii').render(**textconf))
    text = as_ascii(Template(text, output_encoding='ascii').render(**textconf))
    cl_name = "cl_alif_rate" if adaptive else "cl_lif_rate"
    return _plan_template(
        queue, cl_name, text, declares=decs,
        inputs=inputs, outputs=outputs, parameters=parameters, **kwargs)


def plan_rectified_linear(queue, J, R, **kwargs):
    assert J.ctype == 'float'
    assert R.ctype == J.ctype

    textconf = dict(type=J.ctype)
    decs = "const ${type} c0 = 0;"
    text = "R = max(J, c0);"

    decs = as_ascii(Template(decs, output_encoding='ascii').render(**textconf))
    text = as_ascii(Template(text, output_encoding='ascii').render(**textconf))
    cl_name = "cl_rectified_linear"
    return _plan_template(
        queue, cl_name, text, declares=decs,
        inputs=dict(J=J), outputs=dict(R=R), **kwargs)


def plan_sigmoid(queue, J, R, ref, **kwargs):
    assert J.ctype == 'float'
    assert R.ctype == J.ctype

    textconf = dict(type=J.ctype)
    decs = "const ${type} c0 = 0, c1 = 1, t0 = -88, t1 = 15;"
    text = "R = J < t0 ? c0 : J > t1 ? c1/ref : c1 / (ref * (c1 + exp(-J)));"
    # ^ constants for cutoffs from Theano

    decs = as_ascii(Template(decs, output_encoding='ascii').render(**textconf))
    text = as_ascii(Template(text, output_encoding='ascii').render(**textconf))
    cl_name = "cl_sigmoid"
    return _plan_template(
        queue, cl_name, text, declares=decs,
        inputs=dict(J=J), outputs=dict(R=R), parameters=dict(ref=ref),
        **kwargs)


def _plan_template(queue, name, core_text, declares="", tag=None,
                   blockify=True, inputs={}, outputs={}, parameters={}):
    """Template for making a plan for vector nonlinearities.

    This template assumes that all inputs and outputs are vectors.

    Parameters
    ----------
    blockify : bool
        If true, divide the inputs up into blocks with a maximum size.

    inputs: dictionary of CLRaggedArrays
        Inputs to the function. RaggedArrays must be a list of vectors.

    outputs: dictionary of CLRaggedArrays
        Outputs of the function. RaggedArrays must be a list of vectors.

    parameters: dictionary of CLRaggedArrays
        Parameters to the function. Each RaggedArray element must be a vector
        of the same length of the inputs, or a scalar (to be broadcasted).
        Providing a float instead of a RaggedArray makes that parameter
        constant.

    """
    input0 = list(inputs.values())[0]   # input to use as reference for lengths

    # split parameters into static and updated params
    static_params = OrderedDict()  # static params (hard-coded)
    params = OrderedDict()  # variable params (updated)
    for k, v in parameters.items():
        if isinstance(v, CLRaggedArray):
            params[k] = v
        elif is_number(v):
            static_params[k] = ('float', float(v))
        else:
            raise ValueError(
                "Parameter %r must be CLRaggedArray or float (got %s)"
                % (k, type(v)))

    avars = OrderedDict()
    bw_per_call = 0
    for vname, v in (list(inputs.items()) + list(outputs.items()) +
                     list(params.items())):
        assert vname not in avars, "Name clash"
        assert len(v) == len(input0)
        assert (v.shape0s == input0.shape0s).all()
        assert (v.shape1s == 1).all()  # vectors only
        assert (v.stride0s == 1).all()  # contiguous

        offset = '%(name)s_starts[gind1]' % {'name': vname}
        avars[vname] = (v.ctype, offset)
        bw_per_call += v.nbytes

    ivars = OrderedDict((k, avars[k]) for k in inputs.keys())
    ovars = OrderedDict((k, avars[k]) for k in outputs.keys())
    pvars = OrderedDict((k, avars[k]) for k in params.keys())

    fn_name = str(name)
    textconf = dict(fn_name=fn_name, declares=declares, core_text=core_text,
                    ivars=ivars, ovars=ovars, pvars=pvars,
                    static_params=static_params)

    text = """
    ////////// MAIN FUNCTION //////////
    __kernel void ${fn_name}(
% for name, [type, offset] in ivars.items():
        __global const int *${name}_starts,
        __global const ${type} *${name}_buf,
% endfor
% for name, [type, offset] in ovars.items():
        __global const int *${name}_starts,
        __global ${type} *${name}_buf,
% endfor
% for name, [type, offset] in pvars.items():
        __global const int *${name}_starts,
        __global const int *${name}_shape0s,
        __global const ${type} *${name}_buf,
% endfor
        __global const int *sizes
    )
    {
        const int gind0 = get_global_id(0);
        const int gind1 = get_global_id(1);
        if (gind1 >= ${N} || gind0 >= sizes[gind1])
            return;

% for name, [type, offset] in ivars.items():
        ${type} ${name} = ${name}_buf[${offset} + gind0];
% endfor
% for name, [type, offset] in ovars.items():
        ${type} ${name};
% endfor
% for name, [type, offset] in pvars.items():
        const ${type} ${name} = ${name}_buf[${offset} + gind0];
% endfor
% for name, [type, value] in static_params.items():
        const ${type} ${name} = ${value};
% endfor
        //////////////////////////////////////////////////
        //vvvvv USER DECLARATIONS BELOW vvvvv
        ${declares}
        //^^^^^ USER DECLARATIONS ABOVE ^^^^^
        //////////////////////////////////////////////////

        /////vvvvv USER COMPUTATIONS BELOW vvvvv
        ${core_text}
        /////^^^^^ USER COMPUTATIONS ABOVE ^^^^^

% for name, [type, offset] in ovars.items():
        ${name}_buf[${offset} + gind0] = ${name};
% endfor
    }
    """

    if blockify:
        # blockify to help with heterogeneous sizes

        # find best block size
        block_sizes = [16, 32, 64, 128, 256, 512, 1024]
        N = np.inf
        for block_size_i in block_sizes:
            sizes_i, inds_i, _ = blockify_vector(block_size_i, input0)
            if len(sizes_i) < N:
                N = len(sizes_i)
                block_size = block_size_i
                sizes = sizes_i
                inds = inds_i

        clsizes = to_device(queue, sizes)
        get_starts = lambda ras: [to_device(queue, starts) for starts in
                                  blockify_vectors(block_size, ras)[2]]
        Istarts = get_starts(itervalues(inputs))
        Ostarts = get_starts(itervalues(outputs))
        Pstarts = get_starts(itervalues(params))
        Pshape0s = [
            to_device(queue, x.shape0s[inds]) for x in itervalues(params)]

        lsize = None
        gsize = (block_size, len(sizes))

        full_args = []
        for vstarts, v in zip(Istarts, itervalues(inputs)):
            full_args.extend([vstarts, v.cl_buf])
        for vstarts, v in zip(Ostarts, itervalues(outputs)):
            full_args.extend([vstarts, v.cl_buf])
        for vstarts, vshape0s, v in zip(Pstarts, Pshape0s, itervalues(params)):
            full_args.extend([vstarts, vshape0s, v.cl_buf])
        full_args.append(clsizes)
    else:
        # Allocate more than enough kernels in a matrix
        lsize = None
        gsize = (input0.shape0s.max(), len(input0))

        full_args = []
        for v in itervalues(inputs):
            full_args.extend([v.cl_starts, v.cl_buf])
        for v in itervalues(outputs):
            full_args.extend([v.cl_starts, v.cl_buf])
        for vname, v in params.items():
            full_args.extend([v.cl_starts, v.cl_shape0s, v.cl_buf])
        full_args.append(input0.cl_shape0s)

    textconf['N'] = gsize[1]
    text = as_ascii(Template(text, output_encoding='ascii').render(**textconf))
    fns = cl.Program(queue.context, text).build()
    _fn = getattr(fns, fn_name)
    _fn.set_args(*[arr.data for arr in full_args])

    plan = Plan(queue, _fn, gsize, lsize=lsize, name=name, tag=tag)
    plan.full_args = tuple(full_args)  # prevent garbage-collection
    plan.bw_per_call = bw_per_call
    plan.description = ("groups: %d; items: %d; items/group: %0.1f [%d, %d]" %
                        (gsize[1], input0.sizes.sum(), input0.sizes.mean(),
                         input0.sizes.min(), input0.sizes.max()))
    return plan


def create_rngs(queue, n):
    # max 32 states per RNG to save memory (many processes just need a few)
    work_items = get_mwgs(queue, cap=32)
    rngs = CLRaggedArray.from_arrays(
        queue, [np.zeros((work_items, 28), dtype=np.int32)] * n)
    return rngs


_init_rng_kernel = None


def init_rngs(queue, rngs, seeds):
    assert len(seeds) == len(rngs)
    assert np.all(rngs.shape0s == rngs.shape0s[0])
    assert np.all(rngs.shape1s == 28)

    global _init_rng_kernel
    if _init_rng_kernel is None:
        text = """
            #define RANLUXCL_LUX 2  // do not need highest quality
            #include "pyopencl-ranluxcl.cl"

            ////////// MAIN FUNCTION //////////
            __kernel void init_rng(
                __global const uint *seeds,
                __global const int *rng_starts,
                __global int *rng_data
            )
            {
                const int i = get_global_id(0);
                const int k = get_global_id(1);

                // scale seed by 2**32 (see pyopencl-ranluxcl.cl)
                ulong x = (ulong)i + (ulong)seeds[k] * ((ulong)UINT_MAX + 1);
                __global ranluxcl_state_t *rng = rng_data + rng_starts[k];
                ranluxcl_init(x, rng + i);
            }
            """
        text = as_ascii(Template(text, output_encoding='ascii').render())
        _init_rng_kernel = cl.Program(queue.context, text).build().init_rng

    cl_seeds = to_device(queue, np.array(seeds, dtype=np.uint32))
    args = (cl_seeds, rngs.cl_starts, rngs.cl_buf)

    rng_items = rngs.shape0s[0]
    gsize = (int(rng_items), len(rngs))
    lsize = None
    e = _init_rng_kernel(queue, gsize, lsize, *[arr.data for arr in args])
    e.wait()


_dist_enums = {nengod.Uniform: 0, nengod.Gaussian: 1}
_dist_params = {
    nengod.Uniform: lambda d: np.array([d.low, d.high], dtype=np.float32),
    nengod.Gaussian: lambda d: np.array([d.mean, d.std], dtype=np.float32),
    }
dist_header = """
#include "pyopencl-ranluxcl.cl"

inline float4 sample_dist(
    int dist, __global const float *params, ranluxcl_state_t *state)
{
    switch (dist) {
        case 0:  // Uniform (params: low, high)
            //return ranluxcl32(state);
            return params[0] + (params[1] - params[0]) * ranluxcl32(state);
        case 1:  // Gaussian (params: mean, std)
            //return 0.0f;
            return params[0] + params[1] * ranluxcl32norm(state);
        default:
            return 0.0f;
    }
}

inline float getfloat4(float4 a, int i) {
    switch (i) {
        case 0: return a.s0;
        case 1: return a.s1;
        case 2: return a.s2;
        case 3: return a.s3;
    }
}
"""


def get_dist_enums_params(dists):
    enums = [_dist_enums[d.__class__] for d in dists]
    params = [_dist_params[d.__class__](d) for d in dists]
    return (RaggedArray(enums, dtype=np.int32),
            RaggedArray(params, dtype=np.float32))


def plan_whitenoise(queue, Y, dist_enums, dist_params, scale, inc, dt, rngs,
                    tag=None):
    N = len(Y)
    assert N == len(dist_enums) == len(dist_params) == scale.size == inc.size

    assert dist_enums.ctype == 'int'
    assert scale.ctype == inc.ctype == 'int'

    for arr in (Y, dist_enums, dist_params):
        assert (arr.shape1s == 1).all()  # vectors
        assert (arr.stride0s == 1).all()  # contiguous

    assert (dist_enums.shape0s == 1).all()

    text = """
        ${dist_header}

        ////////// MAIN FUNCTION //////////
        __kernel void whitenoise(
            __global const int *shape0s,
            __global const int *Ystarts,
            __global ${Ytype} *Ydata,
            __global const int *Estarts,
            __global const int *Edata,
            __global const int *Pstarts,
            __global const ${Ptype} *Pdata,
            __global const int *scales,
            __global const int *incs,
            __global const int *rng_starts,
            __global int *rng_data
        )
        {
            const int i0 = get_global_id(0);
            const int k = get_global_id(1);
            const int m = shape0s[k];
            if (i0 >= m)
                return;

            __global ${Ytype} *y = Ydata + Ystarts[k];

            __global ranluxcl_state_t *gstate = rng_data + rng_starts[k];
            ranluxcl_state_t state = gstate[i0];

            const int scale = scales[k];
            const int inc = incs[k];
            const int dist_enum = *(Edata + Estarts[k]);
            __global const float *dist_params = Pdata + Pstarts[k];

            float4 samples;
            float sample;
            int samplei = 4;
            for (int i = i0; i < m; i += get_global_size(0))
            {
                if (samplei >= 4) {
                    samples = sample_dist(dist_enum, dist_params, &state);
                    samplei = 0;
                }

                sample = getfloat4(samples, samplei);
                if (scale) sample *= ${sqrt_dt_inv};
                if (inc) y[i] += sample; else y[i] = sample;
                samplei++;
            }

            gstate[i0] = state;
        }
        """

    textconf = dict(Ytype=Y.ctype, Ptype=dist_params.ctype,
                    sqrt_dt_inv=1 / np.sqrt(dt), dist_header=dist_header)
    text = as_ascii(Template(text, output_encoding='ascii').render(**textconf))

    full_args = (
        Y.cl_shape0s,
        Y.cl_starts,
        Y.cl_buf,
        dist_enums.cl_starts,
        dist_enums.cl_buf,
        dist_params.cl_starts,
        dist_params.cl_buf,
        scale,
        inc,
        rngs.cl_starts,
        rngs.cl_buf,
    )
    _fn = cl.Program(queue.context, text).build().whitenoise
    _fn.set_args(*[arr.data for arr in full_args])

    max_len = min(min(rngs.shape0s), max(Y.shape0s))
    gsize = (max_len, N)
    lsize = (max_len, 1)
    plan = Plan(queue, _fn, gsize, lsize=lsize, name="cl_whitenoise", tag=tag)
    plan.full_args = full_args     # prevent garbage-collection
    return plan


def plan_presentinput(queue, Y, t, signals, dt, pres_t=None, tag=None):
    N = len(Y)
    assert len(Y) == len(t) == len(signals)
    assert pres_t is None or pres_t.shape == (N,)

    assert (t.shape0s == 1).all() and (t.shape1s == 1).all()  # scalar
    assert (Y.shape1s == 1).all()  # vector
    for arr in [Y, signals]:
        assert (arr.stride0s == 1).all()  # contiguous columns

    assert (Y.shape0s == signals.shape0s).all()

    text = """
        ////////// MAIN FUNCTION //////////
        __kernel void presentinput(
    % if Ptype is not None:
            __global ${Ptype} *Pdata,
    % endif
            __global const int *Yshape0s,
            __global const int *Ystarts,
            __global ${Ytype} *Ydata,
            __global const int *Tstarts,
            __global ${Ttype} *Tdata,
            __global const int *Sshape1s,
            __global const int *Sstride1s,
            __global const int *Sstarts,
            __global ${Stype} *Sdata
        )
        {
            int i = get_global_id(0);
            const int k = get_global_id(1);
            const int m = Yshape0s[k];
            if (i >= m)
                return;

            __global ${Ytype} *y = Ydata + Ystarts[k];
            __global ${Ytype} *s = Sdata + Sstarts[k];
            const int it = *(Tdata + Tstarts[k]);
            const int nt = ${Sshape1};
    % if Ptype is not None:
            const float pt = Pdata[k];
            const int ti = (int)((it - 0.5f) * (${dt}f / pt)) % nt;
    % else:
            const int ti = (int)it % nt;
    % endif

            for (; i < m; i += get_global_size(0))
                y[i] = s[i + ti*${Sstride1}];
        }
        """

    textconf = dict(Ytype=Y.ctype, Ttype=t.ctype, Stype=signals.ctype,
                    Ptype=pres_t.ctype if pres_t is not None else None,
                    dt=dt)
    update_geometry(textconf, signals, 'S', index='k')
    text = as_ascii(Template(text, output_encoding='ascii').render(**textconf))

    full_args = ((pres_t,) if pres_t is not None else ()) + (
        Y.cl_shape0s,
        Y.cl_starts,
        Y.cl_buf,
        t.cl_starts,
        t.cl_buf,
        signals.cl_shape1s,
        signals.cl_stride1s,
        signals.cl_starts,
        signals.cl_buf,
    )
    _fn = cl.Program(queue.context, text).build().presentinput
    _fn.set_args(*[arr.data for arr in full_args])

    max_len = min(max(Y.shape0s), get_mwgs(queue))
    gsize = (max_len, N)
    lsize = (max_len, 1)
    plan = Plan(
        queue, _fn, gsize, lsize=lsize, name="cl_presentinput", tag=tag)
    plan.full_args = full_args     # prevent garbage-collection
    return plan


def plan_conv2d(queue, X, Y, filters, biases, shape_in, shape_out,
                kernel_shape, conv, padding, strides,
                tag=None, transposed=False):
    """
    Parameters
    ----------
        filters = ch x size_i x size_j x nf             # conv transposed
        filters = ch x size_i x size_j x nf x ni x nj   # local transposed
        biases = nf x ni x nj

        conv : whether this is a convolution (true) or local filtering (false)
    """
    # TODO: re-optimize for col-major
    for ary in [X, Y, filters, biases]:
        # assert that arrays are contiguous
        assert len(ary.shape) in [1, 2]
        assert ary.strides[-1] == ary.dtype.itemsize
        if len(ary.shape) == 2:
            assert ary.strides[0] == ary.dtype.itemsize * ary.shape[1]

    assert filters.start == biases.start == 0
    assert X.ctype == Y.ctype == filters.ctype == biases.ctype

    text = """
    __kernel void conv2d(
        __global const ${type} *x,
        __global const ${type} *f,
        __global const ${type} *b,
        __global ${type} *y
    )
    {
        const int j = get_global_id(0);
        const int i = get_global_id(1);
        const int k = get_global_id(2);
        const int ij = i*${nyj} + j;

        const int tj = get_local_id(0);
        const int ti = get_local_id(1);
        const int lsizej = get_local_size(0);
        const int lsizei = get_local_size(1);
        const int lsize = lsizei * lsizej;
        const int tij = ti*lsizej + tj;
        const int j0 = (j - tj)*${stj} - ${pj};
        const int i0 = (i - ti)*${sti} - ${pi};
        __local ${type} patch[${nipatch}][${njpatch}];
    % if conv:
        __local ${type} filter[${si*sj}];
    % else:
        f += ij;
    % endif
        x += ${xstart};
        y += ${ystart};

        ${type} out = b[k*${nyi*nyj} + ij];

        for (int c = 0; c < ${nc}; c++) {

            // load image section
            __global const ${type} *xc = &x[c * ${nxi * nxj}];
            for (int kij = tij; kij < ${npatch}; kij += lsize) {
                const int ki = kij / ${njpatch};
                const int kj = kij % ${njpatch};
                const int ii = i0 + ki;
                const int jj = j0 + kj;
                if (ii >= 0 && ii < ${nxi} && jj >= 0 && jj < ${nxj})
                    patch[ki][kj] = xc[ii*${nxj} + jj];
                else
                    patch[ki][kj] = 0;
            }

    % if conv:
            // load filters
            __global const ${type} *fc = f + k*${nc*si*sj} + c*${si*sj};
            for (int kij = tij; kij < ${si*sj}; kij += lsize) {
                filter[kij] = fc[kij];
            }
    % endif
            barrier(CLK_LOCAL_MEM_FENCE);

            for (int ii = 0; ii < ${si}; ii++)
            for (int jj = 0; jj < ${sj}; jj++)
    % if conv:
                out += filter[ii*${sj}+jj] * patch[${sti}*ti+ii][${stj}*tj+jj];
    % else:
                out += f[((k*${nc} + c)*${si*sj} + ii*${sj} + jj)*${nyi*nyj}]
                       * patch[${sti}*ti+ii][${stj}*tj+jj];
    % endif

            barrier(CLK_LOCAL_MEM_FENCE);
        }

        if (i < ${nyi} && j < ${nyj})
            y[k*${nyi*nyj} + ij] = out;
    }
    """

    nc, nxi, nxj = shape_in
    nf, nyi, nyj = shape_out
    si, sj = kernel_shape
    pi, pj = padding
    sti, stj = strides

    max_group = get_mwgs(queue, cap=256)
    assert max_group >= 32
    lsize0 = min(nyj, 32)
    lsize1 = min(max_group // lsize0, nyi)
    lsize = (lsize0, lsize1, 1)
    gsize = (round_up(nyj, lsize[0]), round_up(nyi, lsize[1]), nf)

    njpatch = (lsize[0] - 1) * stj + sj
    nipatch = (lsize[1] - 1) * sti + si
    npatch = nipatch * njpatch

    assert np.prod(lsize) <= queue.device.max_work_group_size
    assert (npatch*X.dtype.itemsize + conv*si*sj*filters.dtype.itemsize
            <= queue.device.local_mem_size)

    textconf = dict(
        type=X.ctype, conv=conv, nf=nf, nxi=nxi, nxj=nxj, nyi=nyi, nyj=nyj,
        nc=nc, si=si, sj=sj, pi=pi, pj=pj, sti=sti, stj=stj,
        nipatch=nipatch, njpatch=njpatch, npatch=npatch,
        xstart=X.start, ystart=Y.start)
    text = as_ascii(Template(text, output_encoding='ascii').render(**textconf))

    full_args = (X.base_data, filters.data, biases.data, Y.base_data)
    _fn = cl.Program(queue.context, text).build().conv2d
    _fn.set_args(*full_args)

    plan = Plan(queue, _fn, gsize, lsize=lsize, name="cl_conv2d", tag=tag)
    plan.full_args = full_args     # prevent garbage-collection
    plan.flops_per_call = 2 * nyi * nyj * nf * nc * si * sj
    plan.bw_per_call = X.nbytes + filters.nbytes + biases.nbytes + Y.nbytes
    plan.description = "shape_in=%s, shape_out=%s, kernel=%s, conv=%s" % (
        shape_in, shape_out, kernel_shape, conv)
    return plan


def plan_pool2d(queue, X, Y, shape, pool_size, strides, tag=None):
    # TODO: re-optimize for col-major
    for ary in [X, Y]:
        # assert that arrays are contiguous
        assert len(ary.shape) in [1, 2]
        assert ary.strides[-1] == ary.dtype.itemsize
        if len(ary.shape) == 2:
            assert ary.strides[0] == ary.dtype.itemsize * ary.shape[1]

    assert X.ctype == Y.ctype

    text = """
    ////////// MAIN FUNCTION //////////
    __kernel void pool2d(
        __global const ${type} *x,
        __global ${type} *y
    )
    {
        const int j = get_global_id(0);
        const int i = get_global_id(1);
        const int c = get_global_id(2);

        const int tj = get_local_id(0);
        const int ti = get_local_id(1);
        const int lsizej = get_local_size(0);
        const int lsizei = get_local_size(1);
        const int lsize = lsizei * lsizej;
        const int tij = ti*lsizej + tj;
        const int i0 = i - ti;
        const int j0 = j - tj;
        __local ${type} patch[${nipatch}][${njpatch}];

        x += ${Xstart};
        y += ${Ystart};

        // load image patch
        __global const ${type} *xc = &x[c * ${nxi * nxj}];
        for (int kij = tij; kij < ${nipatch * njpatch}; kij += lsize) {
            const int ki = kij / ${njpatch};
            const int kj = kij % ${njpatch};
            const int ii = i0*${sti} + ki;
            const int jj = j0*${stj} + kj;
            if (ii >= 0 && ii < ${nxi} && jj >= 0 && jj < ${nxj})
                patch[ki][kj] = xc[ii*${nxj} + jj];
            else
                patch[ki][kj] = NAN;
        }

        barrier(CLK_LOCAL_MEM_FENCE);

        ${type} out = 0;
        int n = 0;
        ${type} xij;
        for (int ii = 0; ii < ${si}; ii++) {
        for (int jj = 0; jj < ${sj}; jj++) {
            xij = patch[ti*${sti} + ii][tj*${stj} + jj];
            if (!isnan(xij)) {
                out += xij;
                n++;
            }
        }
        }

        if (i < ${nyi} && j < ${nyj})
            y[c*${nyi * nyj} + i*${nyj} + j] = out / n;
    }
    """
    nc, nyi, nyj, nxi, nxj = shape
    si, sj = pool_size
    sti, stj = strides

    max_group = get_mwgs(queue, cap=64)
    assert max_group >= 32
    lsize0 = min(nyj, 8)
    lsize1 = min(nyi, max_group // lsize0)
    lsize = (lsize0, lsize1, 1)
    gsize = (round_up(nyj, lsize[0]), round_up(nyi, lsize[1]), nc)

    njpatch = lsize[0]*sti + si - 1
    nipatch = lsize[1]*stj + sj - 1
    assert nipatch*njpatch <= queue.device.local_mem_size / X.dtype.itemsize

    textconf = dict(
        type=X.ctype, Xstart=X.start, Ystart=Y.start,
        nxi=nxi, nxj=nxj, nyi=nyi, nyj=nyj, si=si, sj=sj, sti=sti, stj=stj,
        nipatch=nipatch, njpatch=njpatch)

    text = as_ascii(Template(text, output_encoding='ascii').render(**textconf))

    full_args = (X.base_data, Y.base_data)
    _fn = cl.Program(queue.context, text).build().pool2d
    _fn.set_args(*full_args)

    plan = Plan(queue, _fn, gsize, lsize=lsize, name="cl_pool2d", tag=tag)
    plan.full_args = full_args     # prevent garbage-collection
    plan.flops_per_call = X.size
    plan.bw_per_call = X.nbytes + Y.nbytes
    return plan


def plan_bcm(queue, pre, post, theta, delta, alpha, tag=None):
    assert len(pre) == len(post) == len(theta) == len(delta) == alpha.size
    N = len(pre)

    for arr in (pre, post, theta):  # vectors
        assert (arr.shape1s == 1).all()

    assert (post.shape0s == delta.shape0s).all()
    assert (pre.shape0s == delta.shape1s).all()
    assert (post.shape0s == theta.shape0s).all()

    assert (pre.ctype == post.ctype == theta.ctype == delta.ctype ==
            alpha.ctype)

    text = """
    __kernel void bcm(
        __global const int *shape0s,
        __global const int *shape1s,
        __global const int *pre_stride0s,
        __global const int *pre_starts,
        __global const ${type} *pre_data,
        __global const int *post_stride0s,
        __global const int *post_starts,
        __global const ${type} *post_data,
        __global const int *theta_stride0s,
        __global const int *theta_starts,
        __global const ${type} *theta_data,
        __global const int *delta_stride0s,
        __global const int *delta_stride1s,
        __global const int *delta_starts,
        __global ${type} *delta_data,
        __global const ${type} *alphas
    )
    {
        const int ij = get_global_id(0);
        const int k = get_global_id(1);

        const int shape0 = shape0s[k];
        const int shape1 = shape1s[k];
        const int i = ij % shape0;
        const int j = ij / shape0;

        __global ${type} *delta = delta_data + delta_starts[k];
        const ${type} pre = pre_data[pre_starts[k] + j*${pre_stride0}];
        const ${type} post = post_data[post_starts[k] + i*${post_stride0}];
        const ${type} theta = theta_data[
            theta_starts[k] + i*${theta_stride0}];
        const ${type} alpha = alphas[k];

        if (j < shape1) {
            delta[i*${delta_stride0} + j*${delta_stride1}] =
                alpha * post * (post - theta) * pre;
        }
    }
    """

    textconf = dict(type=pre.ctype)
    update_geometry(textconf, pre, 'pre_', index='k')
    update_geometry(textconf, post, 'post_', index='k')
    update_geometry(textconf, theta, 'theta_', index='k')
    update_geometry(textconf, delta, 'delta_', index='k')
    text = as_ascii(Template(text, output_encoding='ascii').render(**textconf))

    full_args = (
        delta.cl_shape0s, delta.cl_shape1s,
        pre.cl_stride0s, pre.cl_starts, pre.cl_buf,
        post.cl_stride0s, post.cl_starts, post.cl_buf,
        theta.cl_stride0s, theta.cl_starts, theta.cl_buf,
        delta.cl_stride0s, delta.cl_stride1s, delta.cl_starts, delta.cl_buf,
        alpha,
    )
    _fn = cl.Program(queue.context, text).build().bcm
    _fn.set_args(*[arr.data for arr in full_args])

    lsize = None
    gsize = (delta.sizes.max(), N)
    plan = Plan(queue, _fn, gsize, lsize=lsize, name="cl_bcm", tag=tag)
    plan.full_args = full_args     # prevent garbage-collection
    plan.flops_per_call = 4 * delta.sizes.sum()
    plan.bw_per_call = (pre.nbytes + post.nbytes + theta.nbytes +
                        delta.nbytes + alpha.nbytes)
    return plan


def plan_oja(queue, pre, post, weights, delta, alpha, beta, tag=None):
    assert (len(pre) == len(post) == len(weights) == len(delta) ==
            alpha.size == beta.size)
    N = len(pre)

    for arr in (pre, post):  # vectors
        assert (arr.shape1s == 1).all()

    assert (post.shape0s == weights.shape0s).all()
    assert (pre.shape0s == weights.shape1s).all()
    assert (weights.shape0s == delta.shape0s).all()
    assert (weights.shape1s == delta.shape1s).all()

    assert (pre.ctype == post.ctype == weights.ctype == delta.ctype ==
            alpha.ctype == beta.ctype)

    text = """
    __kernel void oja(
        __global const int *shape0s,
        __global const int *shape1s,
        __global const int *pre_stride0s,
        __global const int *pre_starts,
        __global const ${type} *pre_data,
        __global const int *post_stride0s,
        __global const int *post_starts,
        __global const ${type} *post_data,
        __global const int *weights_stride0s,
        __global const int *weights_stride1s,
        __global const int *weights_starts,
        __global const ${type} *weights_data,
        __global const int *delta_stride0s,
        __global const int *delta_stride1s,
        __global const int *delta_starts,
        __global ${type} *delta_data,
        __global const ${type} *alphas,
        __global const ${type} *betas
    )
    {
        const int ij = get_global_id(0);
        const int k = get_global_id(1);

        const int shape0 = shape0s[k];
        const int shape1 = shape1s[k];
        const int i = ij % shape0;
        const int j = ij / shape0;

        __global ${type} *delta = delta_data + delta_starts[k];
        const ${type} pre = pre_data[pre_starts[k] + j*${pre_stride0}];
        const ${type} post = post_data[post_starts[k] + i*${post_stride0}];
        const ${type} weight = weights_data[
            weights_starts[k] + i*${weights_stride0} + j*${weights_stride1}];
        const ${type} alpha = alphas[k];
        const ${type} beta = betas[k];

        if (j < shape1) {
            delta[i*${delta_stride0} + j*${delta_stride1}] =
                alpha * post * (pre - beta * weight * post);
        }
    }
    """

    textconf = dict(type=pre.ctype)
    update_geometry(textconf, pre, 'pre_', index='k')
    update_geometry(textconf, post, 'post_', index='k')
    update_geometry(textconf, weights, 'weights_', index='k')
    update_geometry(textconf, delta, 'delta_', index='k')
    text = as_ascii(Template(text, output_encoding='ascii').render(**textconf))

    full_args = (
        delta.cl_shape0s, delta.cl_shape1s,
        pre.cl_stride0s, pre.cl_starts, pre.cl_buf,
        post.cl_stride0s, post.cl_starts, post.cl_buf,
        weights.cl_stride0s, weights.cl_stride1s, weights.cl_starts,
        weights.cl_buf,
        delta.cl_stride0s, delta.cl_stride1s, delta.cl_starts, delta.cl_buf,
        alpha, beta,
    )
    _fn = cl.Program(queue.context, text).build().oja
    _fn.set_args(*[arr.data for arr in full_args])

    lsize = None
    gsize = (delta.sizes.max(), N)
    plan = Plan(queue, _fn, gsize, lsize=lsize, name="cl_oja", tag=tag)
    plan.full_args = full_args     # prevent garbage-collection
    plan.flops_per_call = 6 * delta.sizes.sum()
    plan.bw_per_call = (pre.nbytes + post.nbytes + weights.nbytes +
                        delta.nbytes + alpha.nbytes + beta.nbytes)
    return plan


def plan_voja(queue, pre, post, enc, delta, learn, scale, alpha, tag=None):
    assert (len(pre) == len(post) == len(enc) == len(delta) ==
            len(learn) == alpha.size == len(scale))
    N = len(pre)

    for arr in (learn,):  # scalars
        assert (arr.shape0s == 1).all()
        assert (arr.shape1s == 1).all()
    for arr in (pre, post, scale):  # vectors
        assert (arr.shape1s == 1).all()

    assert (post.shape0s == enc.shape0s).all()
    assert (pre.shape0s == enc.shape1s).all()
    assert (enc.shape0s == delta.shape0s).all()
    assert (enc.shape1s == delta.shape1s).all()

    assert (pre.ctype == post.ctype == enc.ctype == delta.ctype ==
            learn.ctype == scale.ctype == alpha.ctype)

    text = """
    __kernel void voja(
        __global const int *shape0s,
        __global const int *shape1s,
        __global const int *pre_stride0s,
        __global const int *pre_starts,
        __global const ${type} *pre_data,
        __global const int *post_stride0s,
        __global const int *post_starts,
        __global const ${type} *post_data,
        __global const int *enc_stride0s,
        __global const int *enc_stride1s,
        __global const int *enc_starts,
        __global const ${type} *enc_data,
        __global const int *delta_stride0s,
        __global const int *delta_stride1s,
        __global const int *delta_starts,
        __global ${type} *delta_data,
        __global const int *learn_starts,
        __global const ${type} *learn_data,
        __global const int *scale_stride0s,
        __global const int *scale_starts,
        __global const ${type} *scale_data,
        __global const ${type} *alphas
    )
    {
        const int ij = get_global_id(0);
        const int k = get_global_id(1);

        const int shape0 = shape0s[k];
        const int shape1 = shape1s[k];
        const int i = ij % shape0;
        const int j = ij / shape0;

        __global ${type} *delta = delta_data + delta_starts[k];
        const ${type} pre = pre_data[pre_starts[k] + j*${pre_stride0}];
        const ${type} post = post_data[post_starts[k] + i*${post_stride0}];
        const ${type} enc = enc_data[
            enc_starts[k] + i*${enc_stride0} + j*${enc_stride1}];
        const ${type} learn = learn_data[learn_starts[k]];
        const ${type} scale = scale_data[scale_starts[k] + i*${scale_stride0}];
        const ${type} alpha = alphas[k];

        if (j < shape1) {
            delta[i*${delta_stride0} + j*${delta_stride1}] =
                alpha * learn * post * (scale * pre - enc);
        }
    }
    """

    textconf = dict(type=pre.ctype)
    update_geometry(textconf, pre, 'pre_', index='k')
    update_geometry(textconf, post, 'post_', index='k')
    update_geometry(textconf, scale, 'scale_', index='k')
    update_geometry(textconf, enc, 'enc_', index='k')
    update_geometry(textconf, delta, 'delta_', index='k')
    text = as_ascii(Template(text, output_encoding='ascii').render(**textconf))

    full_args = (
        delta.cl_shape0s, delta.cl_shape1s,
        pre.cl_stride0s, pre.cl_starts, pre.cl_buf,
        post.cl_stride0s, post.cl_starts, post.cl_buf,
        enc.cl_stride0s, enc.cl_stride1s, enc.cl_starts, enc.cl_buf,
        delta.cl_stride0s, delta.cl_stride1s, delta.cl_starts, delta.cl_buf,
        learn.cl_starts, learn.cl_buf,
        scale.cl_stride0s, scale.cl_starts, scale.cl_buf,
        alpha,
    )
    _fn = cl.Program(queue.context, text).build().voja
    _fn.set_args(*[arr.data for arr in full_args])

    lsize = None
    gsize = (delta.sizes.max(), N)
    plan = Plan(queue, _fn, gsize, lsize=lsize, name="cl_voja", tag=tag)
    plan.full_args = full_args     # prevent garbage-collection
    plan.flops_per_call = 5 * delta.sizes.sum()
    plan.bw_per_call = (pre.nbytes + post.nbytes + enc.nbytes + delta.nbytes +
                        learn.nbytes + scale.nbytes + alpha.nbytes)
    return plan
