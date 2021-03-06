
# Authors: Denis A. Engemann  <denis.engemann@gmail.com>
#          Martin Luessi <mluessi@nmr.mgh.harvard.edu>
#          Alexandre Gramfort <alexandre.gramfort@telecom-paristech.fr>
#          Matti Hamalainen <msh@nmr.mgh.harvard.edu>
#          Yuval Harpaz <yuvharpaz@gmail.com>
#
#          simplified BSD-3 license

import os.path as op
from itertools import count

import numpy as np

from ...utils import logger, verbose, sum_squared
from ...transforms import (combine_transforms, invert_transform, apply_trans,
                           Transform)
from ..constants import FIFF
from .. import _BaseRaw, _coil_trans_to_loc, _loc_to_coil_trans, _empty_info
from ..utils import _mult_cal_one, read_str
from .constants import BTI
from .read import (read_int32, read_int16, read_float, read_double,
                   read_transform, read_char, read_int64, read_uint16,
                   read_uint32, read_double_matrix, read_float_matrix,
                   read_int16_matrix)
from ...externals import six

FIFF_INFO_CHS_FIELDS = ('loc',
                        'ch_name', 'unit_mul', 'coord_frame', 'coil_type',
                        'range', 'unit', 'cal',
                        'scanno', 'kind', 'logno')

FIFF_INFO_CHS_DEFAULTS = (np.array([0, 0, 0, 1] * 3, dtype='f4'),
                          None, 0, 0, 0,
                          1.0, FIFF.FIFF_UNIT_V, 1.0,
                          None, FIFF.FIFFV_ECG_CH, None)

FIFF_INFO_DIG_FIELDS = ('kind', 'ident', 'r', 'coord_frame')
FIFF_INFO_DIG_DEFAULTS = (None, None, None, FIFF.FIFFV_COORD_HEAD)

BTI_WH2500_REF_MAG = ('MxA', 'MyA', 'MzA', 'MxaA', 'MyaA', 'MzaA')
BTI_WH2500_REF_GRAD = ('GxxA', 'GyyA', 'GyxA', 'GzaA', 'GzyA')

dtypes = zip(list(range(1, 5)), ('>i2', '>i4', '>f4', '>f8'))
DTYPES = dict((i, np.dtype(t)) for i, t in dtypes)


class _bytes_io_mock_context():

    def __init__(self, target):
        self.target = target

    def __enter__(self):
        return self.target

    def __exit__(self, type, value, tb):
        pass


def _bti_open(fname, *args, **kwargs):
    """Handle bytes io"""
    if isinstance(fname, six.string_types):
        return open(fname, *args, **kwargs)
    elif isinstance(fname, six.BytesIO):
        return _bytes_io_mock_context(fname)
    else:
        raise RuntimeError('Cannot mock this.')


def _get_bti_dev_t(adjust=0., translation=(0.0, 0.02, 0.11)):
    """Get the general Magnes3600WH to Neuromag coordinate transform

    Parameters
    ----------
    adjust : float | None
        Degrees to tilt x-axis for sensor frame misalignment.
        If None, no adjustment will be applied.
    translation : array-like
        The translation to place the origin of coordinate system
        to the center of the head.

    Returns
    -------
    m_nm_t : ndarray
        4 x 4 rotation, translation, scaling matrix.
    """
    flip_t = np.array([[0., -1., 0.],
                       [1., 0., 0.],
                       [0., 0., 1.]])
    rad = np.deg2rad(adjust)
    adjust_t = np.array([[1., 0., 0.],
                         [0., np.cos(rad), -np.sin(rad)],
                         [0., np.sin(rad), np.cos(rad)]])
    m_nm_t = np.eye(4)
    m_nm_t[:3, :3] = np.dot(flip_t, adjust_t)
    m_nm_t[:3, 3] = translation
    return m_nm_t


def _rename_channels(names, ecg_ch='E31', eog_ch=('E63', 'E64')):
    """Renames appropriately ordered list of channel names

    Parameters
    ----------
    names : list of str
        Lists of 4-D channel names in ascending order

    Returns
    -------
    new : list
        List of names, channel names in Neuromag style
    """
    new = list()
    ref_mag, ref_grad, eog, eeg, ext = [count(1) for _ in range(5)]
    for i, name in enumerate(names, 1):
        if name.startswith('A'):
            name = 'MEG %3.3d' % i
        elif name == 'RESPONSE':
            name = 'STI 013'
        elif name == 'TRIGGER':
            name = 'STI 014'
        elif any(name == k for k in eog_ch):
            name = 'EOG %3.3d' % six.advance_iterator(eog)
        elif name == ecg_ch:
            name = 'ECG 001'
        elif name.startswith('E'):
            name = 'EEG %3.3d' % six.advance_iterator(eeg)
        elif name == 'UACurrent':
            name = 'UTL 001'
        elif name.startswith('M'):
            name = 'RFM %3.3d' % six.advance_iterator(ref_mag)
        elif name.startswith('G'):
            name = 'RFG %3.3d' % six.advance_iterator(ref_grad)
        elif name.startswith('X'):
            name = 'EXT %3.3d' % six.advance_iterator(ext)

        new += [name]

    return new


def _read_head_shape(fname):
    """ Helper Function """

    with _bti_open(fname, 'rb') as fid:
        fid.seek(BTI.FILE_HS_N_DIGPOINTS)
        _n_dig_points = read_int32(fid)
        idx_points = read_double_matrix(fid, BTI.DATA_N_IDX_POINTS, 3)
        dig_points = read_double_matrix(fid, _n_dig_points, 3)

    return idx_points, dig_points


def _get_ctf_head_to_head_t(idx_points):
    """ Helper function """

    fp = idx_points.astype('>f8')
    dp = np.sum(fp[2] * (fp[0] - fp[1]))
    tmp1, tmp2 = sum_squared(fp[2]), sum_squared(fp[0] - fp[1])
    dcos = -dp / np.sqrt(tmp1 * tmp2)
    dsin = np.sqrt(1. - dcos * dcos)
    dt = dp / np.sqrt(tmp2)

    # do the transformation
    t = np.array([[dcos, -dsin, 0., dt],
                  [dsin, dcos, 0., 0.],
                  [0., 0., 1., 0.],
                  [0., 0., 0., 1.]])
    return Transform('ctf_head', 'head', t)


def _flip_fiducials(idx_points_nm):
    # adjust order of fiducials to Neuromag
    # XXX presumably swap LPA and RPA
    idx_points_nm[[1, 2]] = idx_points_nm[[2, 1]]
    return idx_points_nm


def _process_bti_headshape(fname, convert=True, use_hpi=True):
    """Read index points and dig points from BTi head shape file

    Parameters
    ----------
    fname : str
        The absolute path to the head shape file
    use_hpi : bool
        Whether to treat additional hpi coils as digitization points or not.
        If False, hpi coils will be discarded.

    Returns
    -------
    dig : list of dicts
        The list of dig point info structures needed for the fiff info
        structure.
    t : dict
        The transformation that was used.
    """
    idx_points, dig_points = _read_head_shape(fname)
    if convert:
        ctf_head_t = _get_ctf_head_to_head_t(idx_points)
    else:
        ctf_head_t = Transform('ctf_head', 'ctf_head', np.eye(4))

    if dig_points is not None:
        # dig_points = apply_trans(ctf_head_t['trans'], dig_points)
        all_points = np.r_[idx_points, dig_points]
    else:
        all_points = idx_points

    if convert:
        all_points = _convert_hs_points(all_points, ctf_head_t)

    dig = _points_to_dig(all_points, len(idx_points), use_hpi)
    return dig, ctf_head_t


def _convert_hs_points(points, t):
    """convert to Neuromag"""
    points = apply_trans(t['trans'], points)
    points = _flip_fiducials(points).astype(np.float32)
    return points


def _points_to_dig(points, n_idx_points, use_hpi):
    """Put points in info dig structure"""
    idx_idents = list(range(1, 4)) + list(range(1, (n_idx_points + 1) - 3))
    dig = []
    for idx in range(points.shape[0]):
        point_info = dict(zip(FIFF_INFO_DIG_FIELDS, FIFF_INFO_DIG_DEFAULTS))
        point_info['r'] = points[idx]
        if idx < 3:
            point_info['kind'] = FIFF.FIFFV_POINT_CARDINAL
            point_info['ident'] = idx_idents[idx]
        if 2 < idx < n_idx_points and use_hpi:
            point_info['kind'] = FIFF.FIFFV_POINT_HPI
            point_info['ident'] = idx_idents[idx]
        elif idx > 4:
            point_info['kind'] = FIFF.FIFFV_POINT_EXTRA
            point_info['ident'] = (idx + 1) - len(idx_idents)

        if 2 < idx < n_idx_points and not use_hpi:
            pass
        else:
            dig += [point_info]

    return dig


def _convert_coil_trans(coil_trans, dev_ctf_t, bti_dev_t):
    """ Helper Function """
    t = combine_transforms(invert_transform(dev_ctf_t), bti_dev_t,
                           'ctf_head', 'meg')
    t = np.dot(t['trans'], coil_trans)
    return t


def _correct_offset(fid):
    """ Align fid pointer """
    current = fid.tell()
    if ((current % BTI.FILE_CURPOS) != 0):
        offset = current % BTI.FILE_CURPOS
        fid.seek(BTI.FILE_CURPOS - (offset), 1)


def _read_config(fname):
    """Read BTi system config file

    Parameters
    ----------
    fname : str
        The absolute path to the config file

    Returns
    -------
    cfg : dict
        The config blocks found.

    """

    with _bti_open(fname, 'rb') as fid:
        cfg = dict()
        cfg['hdr'] = {'version': read_int16(fid),
                      'site_name': read_str(fid, 32),
                      'dap_hostname': read_str(fid, 16),
                      'sys_type': read_int16(fid),
                      'sys_options': read_int32(fid),
                      'supply_freq': read_int16(fid),
                      'total_chans': read_int16(fid),
                      'system_fixed_gain': read_float(fid),
                      'volts_per_bit': read_float(fid),
                      'total_sensors': read_int16(fid),
                      'total_user_blocks': read_int16(fid),
                      'next_der_chan_no': read_int16(fid)}

        fid.seek(2, 1)

        cfg['checksum'] = read_uint32(fid)
        cfg['reserved'] = read_char(fid, 32)
        cfg['transforms'] = [read_transform(fid) for t in
                             range(cfg['hdr']['total_sensors'])]

        cfg['user_blocks'] = dict()
        for block in range(cfg['hdr']['total_user_blocks']):
            ub = dict()

            ub['hdr'] = {'nbytes': read_int32(fid),
                         'kind': read_str(fid, 20),
                         'checksum': read_int32(fid),
                         'username': read_str(fid, 32),
                         'timestamp': read_int32(fid),
                         'user_space_size': read_int32(fid),
                         'reserved': read_char(fid, 32)}

            _correct_offset(fid)
            kind = ub['hdr'].pop('kind')
            if not kind:  # make sure reading goes right. Should never be empty
                raise RuntimeError('Could not read user block. Probably you '
                                   'acquired data using a BTi version '
                                   'currently not supported. Please contact '
                                   'the mne-python developers.')
            dta, cfg['user_blocks'][kind] = dict(), ub
            if kind in [v for k, v in BTI.items() if k[:5] == 'UB_B_']:
                if kind == BTI.UB_B_MAG_INFO:
                    dta['version'] = read_int32(fid)
                    fid.seek(20, 1)
                    dta['headers'] = list()
                    for hdr in range(6):
                        d = {'name': read_str(fid, 16),
                             'transform': read_transform(fid),
                             'units_per_bit': read_float(fid)}
                        dta['headers'] += [d]
                        fid.seek(20, 1)

                elif kind == BTI.UB_B_COH_POINTS:
                    dta['n_points'] = read_int32(fid)
                    dta['status'] = read_int32(fid)
                    dta['points'] = []
                    for pnt in range(16):
                        d = {'pos': read_double_matrix(fid, 1, 3),
                             'direction': read_double_matrix(fid, 1, 3),
                             'error': read_double(fid)}
                        dta['points'] += [d]

                elif kind == BTI.UB_B_CCP_XFM_BLOCK:
                    dta['method'] = read_int32(fid)
                    # handle difference btw/ linux (0) and solaris (4)
                    size = 0 if ub['hdr']['user_space_size'] == 132 else 4
                    fid.seek(size, 1)
                    dta['transform'] = read_transform(fid)

                elif kind == BTI.UB_B_EEG_LOCS:
                    dta['electrodes'] = []
                    while True:
                        d = {'label': read_str(fid, 16),
                             'location': read_double_matrix(fid, 1, 3)}
                        if not d['label']:
                            break
                        dta['electrodes'] += [d]

                elif kind in [BTI.UB_B_WHC_CHAN_MAP_VER,
                              BTI.UB_B_WHS_SUBSYS_VER]:
                    dta['version'] = read_int16(fid)
                    dta['struct_size'] = read_int16(fid)
                    dta['entries'] = read_int16(fid)

                    fid.seek(8, 1)

                elif kind == BTI.UB_B_WHC_CHAN_MAP:
                    num_channels = None
                    for name, data in cfg['user_blocks'].items():
                        if name == BTI.UB_B_WHC_CHAN_MAP_VER:
                            num_channels = data['entries']
                            break

                    if num_channels is None:
                        raise ValueError('Cannot find block %s to determine '
                                         'number of channels'
                                         % BTI.UB_B_WHC_CHAN_MAP_VER)

                    dta['channels'] = list()
                    for i in range(num_channels):
                        d = {'subsys_type': read_int16(fid),
                             'subsys_num': read_int16(fid),
                             'card_num': read_int16(fid),
                             'chan_num': read_int16(fid),
                             'recdspnum': read_int16(fid)}
                        dta['channels'] += [d]
                        fid.seek(8, 1)

                elif kind == BTI.UB_B_WHS_SUBSYS:
                    num_subsys = None
                    for name, data in cfg['user_blocks'].items():
                        if name == BTI.UB_B_WHS_SUBSYS_VER:
                            num_subsys = data['entries']
                            break

                    if num_subsys is None:
                        raise ValueError('Cannot find block %s to determine'
                                         ' number of subsystems'
                                         % BTI.UB_B_WHS_SUBSYS_VER)

                    dta['subsys'] = list()
                    for sub_key in range(num_subsys):
                        d = {'subsys_type': read_int16(fid),
                             'subsys_num': read_int16(fid),
                             'cards_per_sys': read_int16(fid),
                             'channels_per_card': read_int16(fid),
                             'card_version': read_int16(fid)}

                        fid.seek(2, 1)

                        d.update({'offsetdacgain': read_float(fid),
                                  'squid_type': read_int32(fid),
                                  'timesliceoffset': read_int16(fid),
                                  'padding': read_int16(fid),
                                  'volts_per_bit': read_float(fid)})

                        dta['subsys'] += [d]

                elif kind == BTI.UB_B_CH_LABELS:
                    dta['version'] = read_int32(fid)
                    dta['entries'] = read_int32(fid)
                    fid.seek(16, 1)

                    dta['labels'] = list()
                    for label in range(dta['entries']):
                        dta['labels'] += [read_str(fid, 16)]

                elif kind == BTI.UB_B_CALIBRATION:
                    dta['sensor_no'] = read_int16(fid)
                    fid.seek(2, 1)
                    dta['timestamp'] = read_int32(fid)
                    dta['logdir'] = read_str(fid, 256)

                elif kind == BTI.UB_B_SYS_CONFIG_TIME:
                    # handle difference btw/ linux (256) and solaris (512)
                    size = 256 if ub['hdr']['user_space_size'] == 260 else 512
                    dta['sysconfig_name'] = read_str(fid, size)
                    dta['timestamp'] = read_int32(fid)

                elif kind == BTI.UB_B_DELTA_ENABLED:
                    dta['delta_enabled'] = read_int16(fid)

                elif kind in [BTI.UB_B_E_TABLE_USED, BTI.UB_B_E_TABLE]:
                    dta['hdr'] = {'version': read_int32(fid),
                                  'entry_size': read_int32(fid),
                                  'n_entries': read_int32(fid),
                                  'filtername': read_str(fid, 16),
                                  'n_e_values': read_int32(fid),
                                  'reserved': read_str(fid, 28)}

                    if dta['hdr']['version'] == 2:
                        size = 16
                        dta['ch_names'] = [read_str(fid, size) for ch in
                                           range(dta['hdr']['n_entries'])]
                        dta['e_ch_names'] = [read_str(fid, size) for ch in
                                             range(dta['hdr']['n_e_values'])]

                        rows = dta['hdr']['n_entries']
                        cols = dta['hdr']['n_e_values']
                        dta['etable'] = read_float_matrix(fid, rows, cols)
                    else:  # handle MAGNES2500 naming scheme
                        dta['ch_names'] = ['WH2500'] * dta['hdr']['n_e_values']
                        dta['hdr']['n_e_values'] = 6
                        dta['e_ch_names'] = BTI_WH2500_REF_MAG
                        rows = dta['hdr']['n_entries']
                        cols = dta['hdr']['n_e_values']
                        dta['etable'] = read_float_matrix(fid, rows, cols)

                        _correct_offset(fid)

                elif any([kind == BTI.UB_B_WEIGHTS_USED,
                          kind[:4] == BTI.UB_B_WEIGHT_TABLE]):
                    dta['hdr'] = {'version': read_int32(fid),
                                  'entry_size': read_int32(fid),
                                  'n_entries': read_int32(fid),
                                  'name': read_str(fid, 32),
                                  'description': read_str(fid, 80),
                                  'n_anlg': read_int32(fid),
                                  'n_dsp': read_int32(fid),
                                  'reserved': read_str(fid, 72)}

                    if dta['hdr']['version'] == 2:
                        dta['ch_names'] = [read_str(fid, 16) for ch in
                                           range(dta['hdr']['n_entries'])]
                        dta['anlg_ch_names'] = [read_str(fid, 16) for ch in
                                                range(dta['hdr']['n_anlg'])]

                        dta['dsp_ch_names'] = [read_str(fid, 16) for ch in
                                               range(dta['hdr']['n_dsp'])]

                        rows = dta['hdr']['n_entries']
                        cols = dta['hdr']['n_dsp']
                        dta['dsp_wts'] = read_float_matrix(fid, rows, cols)
                        cols = dta['hdr']['n_anlg']
                        dta['anlg_wts'] = read_int16_matrix(fid, rows, cols)

                    else:  # handle MAGNES2500 naming scheme
                        dta['ch_names'] = ['WH2500'] * dta['hdr']['n_entries']
                        dta['anlg_ch_names'] = BTI_WH2500_REF_MAG[:3]
                        dta['hdr']['n_anlg'] = len(dta['anlg_ch_names'])
                        dta['dsp_ch_names'] = BTI_WH2500_REF_GRAD
                        dta['hdr.n_dsp'] = len(dta['dsp_ch_names'])
                        dta['anlg_wts'] = np.zeros((dta['hdr']['n_entries'],
                                                    dta['hdr']['n_anlg']),
                                                   dtype='i2')
                        dta['dsp_wts'] = np.zeros((dta['hdr']['n_entries'],
                                                   dta['hdr']['n_dsp']),
                                                  dtype='f4')
                        for n in range(dta['hdr']['n_entries']):
                            dta['anlg_wts'][d] = read_int16_matrix(
                                fid, 1, dta['hdr']['n_anlg'])
                            read_int16(fid)
                            dta['dsp_wts'][d] = read_float_matrix(
                                fid, 1, dta['hdr']['n_dsp'])

                        _correct_offset(fid)

                elif kind == BTI.UB_B_TRIG_MASK:
                    dta['version'] = read_int32(fid)
                    dta['entries'] = read_int32(fid)
                    fid.seek(16, 1)

                    dta['masks'] = []
                    for entry in range(dta['entries']):
                        d = {'name': read_str(fid, 20),
                             'nbits': read_uint16(fid),
                             'shift': read_uint16(fid),
                             'mask': read_uint32(fid)}
                        dta['masks'] += [d]
                        fid.seek(8, 1)

            else:
                dta['unknown'] = {'hdr': read_char(fid,
                                  ub['hdr']['user_space_size'])}

            ub.update(dta)  # finally update the userblock data
            _correct_offset(fid)  # after reading.

        cfg['chs'] = list()

        # prepare reading channels
        def dev_header(x):
            return dict(size=read_int32(x), checksum=read_int32(x),
                        reserved=read_str(x, 32))

        for channel in range(cfg['hdr']['total_chans']):
            ch = {'name': read_str(fid, 16),
                  'chan_no': read_int16(fid),
                  'ch_type': read_uint16(fid),
                  'sensor_no': read_int16(fid),
                  'data': dict()}

            fid.seek(2, 1)
            ch.update({'gain': read_float(fid),
                       'units_per_bit': read_float(fid),
                       'yaxis_label': read_str(fid, 16),
                       'aar_val': read_double(fid),
                       'checksum': read_int32(fid),
                       'reserved': read_str(fid, 32)})

            cfg['chs'] += [ch]
            _correct_offset(fid)  # before and after
            dta = dict()
            if ch['ch_type'] in [BTI.CHTYPE_MEG, BTI.CHTYPE_REFERENCE]:
                dev = {'device_info': dev_header(fid),
                       'inductance': read_float(fid),
                       'padding': read_str(fid, 4),
                       'transform': _correct_trans(read_transform(fid)),
                       'xform_flag': read_int16(fid),
                       'total_loops': read_int16(fid)}

                fid.seek(4, 1)
                dev['reserved'] = read_str(fid, 32)
                dta.update({'dev': dev, 'loops': []})
                for loop in range(dev['total_loops']):
                    d = {'position': read_double_matrix(fid, 1, 3),
                         'orientation': read_double_matrix(fid, 1, 3),
                         'radius': read_double(fid),
                         'wire_radius': read_double(fid),
                         'turns': read_int16(fid)}
                    fid.seek(2, 1)
                    d['checksum'] = read_int32(fid)
                    d['reserved'] = read_str(fid, 32)
                    dta['loops'] += [d]

            elif ch['ch_type'] == BTI.CHTYPE_EEG:
                dta = {'device_info': dev_header(fid),
                       'impedance': read_float(fid),
                       'padding': read_str(fid, 4),
                       'transform': read_transform(fid),
                       'reserved': read_char(fid, 32)}

            elif ch['ch_type'] == BTI.CHTYPE_EXTERNAL:
                dta = {'device_info': dev_header(fid),
                       'user_space_size': read_int32(fid),
                       'reserved': read_str(fid, 32)}

            elif ch['ch_type'] == BTI.CHTYPE_TRIGGER:
                dta = {'device_info': dev_header(fid),
                       'user_space_size': read_int32(fid)}
                fid.seek(2, 1)
                dta['reserved'] = read_str(fid, 32)

            elif ch['ch_type'] in [BTI.CHTYPE_UTILITY, BTI.CHTYPE_DERIVED]:
                dta = {'device_info': dev_header(fid),
                       'user_space_size': read_int32(fid),
                       'reserved': read_str(fid, 32)}

            elif ch['ch_type'] == BTI.CHTYPE_SHORTED:
                dta = {'device_info': dev_header(fid),
                       'reserved': read_str(fid, 32)}

            ch.update(dta)  # add data collected
            _correct_offset(fid)  # after each reading

    return cfg


def _read_epoch(fid):
    """Read BTi PDF epoch"""
    out = {'pts_in_epoch': read_int32(fid),
           'epoch_duration': read_float(fid),
           'expected_iti': read_float(fid),
           'actual_iti': read_float(fid),
           'total_var_events': read_int32(fid),
           'checksum': read_int32(fid),
           'epoch_timestamp': read_int32(fid)}

    fid.seek(28, 1)

    return out


def _read_channel(fid):
    """Read BTi PDF channel"""
    out = {'chan_label': read_str(fid, 16),
           'chan_no': read_int16(fid),
           'attributes': read_int16(fid),
           'scale': read_float(fid),
           'yaxis_label': read_str(fid, 16),
           'valid_min_max': read_int16(fid)}

    fid.seek(6, 1)
    out.update({'ymin': read_double(fid),
                'ymax': read_double(fid),
                'index': read_int32(fid),
                'checksum': read_int32(fid),
                'off_flag': read_str(fid, 16),
                'offset': read_float(fid)})

    fid.seek(12, 1)

    return out


def _read_event(fid):
    """Read BTi PDF event"""
    out = {'event_name': read_str(fid, 16),
           'start_lat': read_float(fid),
           'end_lat': read_float(fid),
           'step_size': read_float(fid),
           'fixed_event': read_int16(fid),
           'checksum': read_int32(fid)}

    fid.seek(32, 1)
    _correct_offset(fid)

    return out


def _read_process(fid):
    """Read BTi PDF process"""

    out = {'nbytes': read_int32(fid),
           'process_type': read_str(fid, 20),
           'checksum': read_int32(fid),
           'user': read_str(fid, 32),
           'timestamp': read_int32(fid),
           'filename': read_str(fid, 256),
           'total_steps': read_int32(fid)}

    fid.seek(32, 1)
    _correct_offset(fid)
    out['processing_steps'] = list()
    for step in range(out['total_steps']):
        this_step = {'nbytes': read_int32(fid),
                     'process_type': read_str(fid, 20),
                     'checksum': read_int32(fid)}
        ptype = this_step['process_type']
        if ptype == BTI.PROC_DEFAULTS:
            this_step['scale_option'] = read_int32(fid)

            fid.seek(4, 1)
            this_step['scale'] = read_double(fid)
            this_step['dtype'] = read_int32(fid)
            this_step['selected'] = read_int16(fid)
            this_step['color_display'] = read_int16(fid)

            fid.seek(32, 1)
        elif ptype in BTI.PROC_FILTER:
            this_step['freq'] = read_float(fid)
            fid.seek(32, 1)
        elif ptype in BTI.PROC_BPFILTER:
            this_step['high_freq'] = read_float(fid)
            this_step['low_frew'] = read_float(fid)
        else:
            jump = this_step['user_space_size'] = read_int32(fid)
            fid.seek(32, 1)
            fid.seek(jump, 1)

        out['processing_steps'] += [this_step]
        _correct_offset(fid)

    return out


def _read_assoc_file(fid):
    """Read BTi PDF assocfile"""

    out = {'file_id': read_int16(fid),
           'length': read_int16(fid)}

    fid.seek(32, 1)
    out['checksum'] = read_int32(fid)

    return out


def _read_pfid_ed(fid):
    """Read PDF ed file"""

    out = {'comment_size': read_int32(fid),
           'name': read_str(fid, 17)}

    fid.seek(9, 1)
    out.update({'pdf_number': read_int16(fid),
                'total_events': read_int32(fid),
                'timestamp': read_int32(fid),
                'flags': read_int32(fid),
                'de_process': read_int32(fid),
                'checksum': read_int32(fid),
                'ed_id': read_int32(fid),
                'win_width': read_float(fid),
                'win_offset': read_float(fid)})

    fid.seek(8, 1)

    return out


def _read_coil_def(fid):
    """ Read coil definition """
    coildef = {'position': read_double_matrix(fid, 1, 3),
               'orientation': read_double_matrix(fid, 1, 3),
               'radius': read_double(fid),
               'wire_radius': read_double(fid),
               'turns': read_int16(fid)}

    fid.seek(fid, 2, 1)
    coildef['checksum'] = read_int32(fid)
    coildef['reserved'] = read_str(fid, 32)


def _read_ch_config(fid):
    """Read BTi channel config"""

    cfg = {'name': read_str(fid, BTI.FILE_CONF_CH_NAME),
           'chan_no': read_int16(fid),
           'ch_type': read_uint16(fid),
           'sensor_no': read_int16(fid)}

    fid.seek(fid, BTI.FILE_CONF_CH_NEXT, 1)

    cfg.update({'gain': read_float(fid),
                'units_per_bit': read_float(fid),
                'yaxis_label': read_str(fid, BTI.FILE_CONF_CH_YLABEL),
                'aar_val': read_double(fid),
                'checksum': read_int32(fid),
                'reserved': read_str(fid, BTI.FILE_CONF_CH_RESERVED)})

    _correct_offset(fid)

    # Then the channel info
    ch_type, chan = cfg['ch_type'], dict()
    chan['dev'] = {'size': read_int32(fid),
                   'checksum': read_int32(fid),
                   'reserved': read_str(fid, 32)}
    if ch_type in [BTI.CHTYPE_MEG, BTI.CHTYPE_REF]:
        chan['loops'] = [_read_coil_def(fid) for d in
                         range(chan['dev']['total_loops'])]

    elif ch_type == BTI.CHTYPE_EEG:
        chan['impedance'] = read_float(fid)
        chan['padding'] = read_str(fid, BTI.FILE_CONF_CH_PADDING)
        chan['transform'] = read_transform(fid)
        chan['reserved'] = read_char(fid, BTI.FILE_CONF_CH_RESERVED)

    elif ch_type in [BTI.CHTYPE_TRIGGER, BTI.CHTYPE_EXTERNAL,
                     BTI.CHTYPE_UTILITY, BTI.CHTYPE_DERIVED]:
        chan['user_space_size'] = read_int32(fid)
        if ch_type == BTI.CHTYPE_TRIGGER:
            fid.seek(2, 1)
        chan['reserved'] = read_str(fid, BTI.FILE_CONF_CH_RESERVED)

    elif ch_type == BTI.CHTYPE_SHORTED:
        chan['reserved'] = read_str(fid, BTI.FILE_CONF_CH_RESERVED)

    cfg['chan'] = chan

    _correct_offset(fid)

    return cfg


def _read_bti_header_pdf(pdf_fname):
    """Read header from pdf file"""
    with _bti_open(pdf_fname, 'rb') as fid:
        fid.seek(-8, 2)
        start = fid.tell()
        header_position = read_int64(fid)
        check_value = header_position & BTI.FILE_MASK

        if ((start + BTI.FILE_CURPOS - check_value) <= BTI.FILE_MASK):
            header_position = check_value

        # Check header position for alignment issues
        if ((header_position % 8) != 0):
            header_position += (8 - (header_position % 8))

        fid.seek(header_position, 0)

        # actual header starts here
        info = {'version': read_int16(fid),
                'file_type': read_str(fid, 5),
                'hdr_size': start - header_position,  # add for convenience
                'start': start}

        fid.seek(1, 1)

        info.update({'data_format': read_int16(fid),
                     'acq_mode': read_int16(fid),
                     'total_epochs': read_int32(fid),
                     'input_epochs': read_int32(fid),
                     'total_events': read_int32(fid),
                     'total_fixed_events': read_int32(fid),
                     'sample_period': read_float(fid),
                     'xaxis_label': read_str(fid, 16),
                     'total_processes': read_int32(fid),
                     'total_chans': read_int16(fid)})

        fid.seek(2, 1)
        info.update({'checksum': read_int32(fid),
                     'total_ed_classes': read_int32(fid),
                     'total_associated_files': read_int16(fid),
                     'last_file_index': read_int16(fid),
                     'timestamp': read_int32(fid)})

        fid.seek(20, 1)
        _correct_offset(fid)

        # actual header ends here, so dar seems ok.

        info['epochs'] = [_read_epoch(fid) for epoch in
                          range(info['total_epochs'])]

        info['chs'] = [_read_channel(fid) for ch in
                       range(info['total_chans'])]

        info['events'] = [_read_event(fid) for event in
                          range(info['total_events'])]

        info['processes'] = [_read_process(fid) for process in
                             range(info['total_processes'])]

        info['assocfiles'] = [_read_assoc_file(fid) for af in
                              range(info['total_associated_files'])]

        info['edclasses'] = [_read_pfid_ed(fid) for ed_class in
                             range(info['total_ed_classes'])]

        info['extra_data'] = fid.read(start - fid.tell())
        info['pdf_fname'] = pdf_fname

    info['total_slices'] = sum(e['pts_in_epoch'] for e in
                               info['epochs'])

    info['dtype'] = DTYPES[info['data_format']]
    bps = info['dtype'].itemsize * info['total_chans']
    info['bytes_per_slice'] = bps
    return info


def _read_bti_header(pdf_fname, config_fname, sort_by_ch_name=True):
    """ Read bti PDF header
    """

    info = _read_bti_header_pdf(pdf_fname) if pdf_fname is not None else dict()
    cfg = _read_config(config_fname)
    info['bti_transform'] = cfg['transforms']

    # augment channel list by according info from config.
    # get channels from config present in PDF
    chans = info.get('chs', None)
    if chans is not None:
        chans_cfg = [c for c in cfg['chs'] if c['chan_no']
                     in [c_['chan_no'] for c_ in chans]]

        # check all pdf chanels are present in config
        match = [c['chan_no'] for c in chans_cfg] == \
                [c['chan_no'] for c in chans]

        if not match:
            raise RuntimeError('Could not match raw data channels with'
                               ' config channels. Some of the channels'
                               ' found are not described in config.')
    else:
        chans_cfg = cfg['chs']
        chans = [dict() for d in chans_cfg]

    # transfer channel info from config to channel info
    for ch, ch_cfg in zip(chans, chans_cfg):
        ch['upb'] = ch_cfg['units_per_bit']
        ch['gain'] = ch_cfg['gain']
        ch['name'] = ch_cfg['name']
        if ch_cfg.get('dev', dict()).get('transform', None) is not None:
            ch['loc'] = _coil_trans_to_loc(ch_cfg['dev']['transform'])
        else:
            ch['loc'] = None
        if pdf_fname is not None:
            if info['data_format'] <= 2:  # see DTYPES, implies integer
                ch['cal'] = ch['scale'] * ch['upb'] / float(ch['gain'])
            else:  # float
                ch['cal'] = ch['scale'] * ch['gain']
        else:  # if we are in this mode we don't read data, only channel info.
            ch['cal'] = ch['scale'] = 1.0  # so we put a trivial default value

    if sort_by_ch_name:
        by_index = [(i, d['index']) for i, d in enumerate(chans)]
        by_index.sort(key=lambda c: c[1])
        by_index = [idx[0] for idx in by_index]
        chs = [chans[pos] for pos in by_index]

        sort_by_name_idx = [(i, d['name']) for i, d in enumerate(chs)]
        a_chs = [c for c in sort_by_name_idx if c[1].startswith('A')]
        other_chs = [c for c in sort_by_name_idx if not c[1].startswith('A')]
        sort_by_name_idx = sorted(
            a_chs, key=lambda c: int(c[1][1:])) + sorted(other_chs)

        sort_by_name_idx = [idx[0] for idx in sort_by_name_idx]

        info['chs'] = [chans[pos] for pos in sort_by_name_idx]
        info['order'] = sort_by_name_idx
    else:
        info['chs'] = chans
        info['order'] = np.arange(len(chans))

    # finally add some important fields from the config
    info['e_table'] = cfg['user_blocks'][BTI.UB_B_E_TABLE_USED]
    info['weights'] = cfg['user_blocks'][BTI.UB_B_WEIGHTS_USED]

    return info


def _correct_trans(t):
    """Helper to convert to a transformation matrix"""
    t = np.array(t, np.float64)
    t[:3, :3] *= t[3, :3][:, np.newaxis]  # apply scalings
    t[3, :3] = 0.  # remove them
    assert t[3, 3] == 1.
    return t


class RawBTi(_BaseRaw):
    """ Raw object from 4D Neuroimaging MagnesWH3600 data

    Parameters
    ----------
    pdf_fname : str
        Path to the processed data file (PDF).
    config_fname : str
        Path to system config file.
    head_shape_fname : str | None
        Path to the head shape file.
    rotation_x : float
        Degrees to tilt x-axis for sensor frame misalignment. Ignored
        if convert is True.
    translation : array-like, shape (3,)
        The translation to place the origin of coordinate system
        to the center of the head. Ignored if convert is True.
    convert : bool
        Convert to Neuromag coordinates or not.
    rename_channels : bool
        Whether to keep original 4D channel labels or not. Defaults to True.
    sort_by_ch_name : bool
        Reorder channels according to channel label. 4D channels don't have
        monotonically increasing numbers in their labels. Defaults to True.
    ecg_ch : str | None
        The 4D name of the ECG channel. If None, the channel will be treated
        as regular EEG channel.
    eog_ch : tuple of str | None
        The 4D names of the EOG channels. If None, the channels will be treated
        as regular EEG channels.
    preload : bool or str (default False)
        Preload data into memory for data manipulation and faster indexing.
        If True, the data will be preloaded into memory (fast, requires
        large amount of memory). If preload is a string, preload is the
        file name of a memory-mapped file which is used to store the data
        on the hard drive (slower, requires less memory).

        ..versionadded:: 0.11

    verbose : bool, str, int, or None
        If not None, override default verbose level (see mne.verbose).
    """
    @verbose
    def __init__(self, pdf_fname, config_fname='config',
                 head_shape_fname='hs_file', rotation_x=0.,
                 translation=(0.0, 0.02, 0.11), convert=True,
                 rename_channels=True, sort_by_ch_name=True,
                 ecg_ch='E31', eog_ch=('E63', 'E64'),
                 preload=False, verbose=None):
        info, bti_info = _get_bti_info(
            pdf_fname=pdf_fname, config_fname=config_fname,
            head_shape_fname=head_shape_fname, rotation_x=rotation_x,
            translation=translation, convert=convert, ecg_ch=ecg_ch,
            rename_channels=rename_channels,
            sort_by_ch_name=sort_by_ch_name, eog_ch=eog_ch)
        self.bti_ch_labels = [c['chan_label'] for c in bti_info['chs']]
        # make Raw repr work if we have a BytesIO as input
        if isinstance(pdf_fname, six.BytesIO):
            pdf_fname = repr(pdf_fname)
        super(RawBTi, self).__init__(
            info, preload, filenames=[pdf_fname], raw_extras=[bti_info],
            last_samps=[bti_info['total_slices'] - 1], verbose=verbose)

    def _read_segment_file(self, data, idx, fi, start, stop, cals, mult):
        """Read a segment of data from a file"""
        bti_info = self._raw_extras[fi]
        fname = bti_info['pdf_fname']
        dtype = bti_info['dtype']
        n_channels = self.info['nchan']
        n_bytes = np.dtype(dtype).itemsize
        data_left = (stop - start) * n_channels
        read_cals = np.empty((bti_info['total_chans'],))
        for ch in bti_info['chs']:
            read_cals[ch['index']] = ch['cal']

        block_size = ((int(100e6) // n_bytes) // n_channels) * n_channels
        block_size = min(data_left, block_size)
        # extract data in chunks
        with _bti_open(fname, 'rb') as fid:
            fid.seek(bti_info['bytes_per_slice'] * start, 0)
            for sample_start in np.arange(0, data_left,
                                          block_size) // n_channels:
                count = min(block_size, data_left - sample_start * n_channels)
                if isinstance(fid, six.BytesIO):
                    block = np.fromstring(fid.getvalue(), dtype, count)
                else:
                    block = np.fromfile(fid, dtype, count)
                sample_stop = sample_start + count // n_channels
                shape = (sample_stop - sample_start, bti_info['total_chans'])
                block.shape = shape
                data_view = data[:, sample_start:sample_stop]
                one = np.empty(block.shape[::-1])

                for ii, b_i_o in enumerate(bti_info['order']):
                    one[ii] = block[:, b_i_o] * read_cals[b_i_o]
                _mult_cal_one(data_view, one, idx, cals, mult)


def _get_bti_info(pdf_fname, config_fname, head_shape_fname, rotation_x,
                  translation, convert, ecg_ch, eog_ch, rename_channels=True,
                  sort_by_ch_name=True):
    """Helper to read BTI info

    Note. This helper supports partial construction of infos when `pdf_fname`
    is None. Some datasets, such as the HCP, are shipped as a large collection
    of zipped files where it can be more efficient to only read the needed
    information. In such a situation, some information can neither be accessed
    directly nor guessed based on the `config`.

    These fields will thus be set to None:
        - 'lowpass'
        - 'highpass'
        - 'sfreq'
        - 'meas_date'

    """
    if pdf_fname is None:
        logger.info('No pdf_fname passed, trying to construct partial info '
                    'from config')
    if pdf_fname is not None and not isinstance(pdf_fname, six.BytesIO):
        if not op.isabs(pdf_fname):
            pdf_fname = op.abspath(pdf_fname)

    if not isinstance(config_fname, six.BytesIO):
        if not op.isabs(config_fname):
            config_fname = op.abspath(config_fname)

        if not op.exists(config_fname):
            raise ValueError('Could not find the config file %s. Please check'
                             ' whether you are in the right directory '
                             'or pass the full name' % config_fname)

    if head_shape_fname is not None and not isinstance(
            head_shape_fname, six.BytesIO):
        orig_name = head_shape_fname
        if not op.isfile(head_shape_fname):
            head_shape_fname = op.join(op.dirname(pdf_fname),
                                       head_shape_fname)

        if not op.isfile(head_shape_fname):
            raise ValueError('Could not find the head_shape file "%s". '
                             'You should check whether you are in the '
                             'right directory or pass the full file name.'
                             % orig_name)

    logger.info('Reading 4D PDF file %s...' % pdf_fname)
    bti_info = _read_bti_header(
        pdf_fname, config_fname, sort_by_ch_name=sort_by_ch_name)

    dev_ctf_t = Transform('ctf_meg', 'ctf_head',
                          _correct_trans(bti_info['bti_transform'][0]))

    # for old backward compatibility and external processing
    rotation_x = 0. if rotation_x is None else rotation_x
    if convert:
        bti_dev_t = _get_bti_dev_t(rotation_x, translation)
    else:
        bti_dev_t = np.eye(4)
    bti_dev_t = Transform('ctf_meg', 'meg', bti_dev_t)

    use_hpi = False  # hard coded, but marked as later option.
    logger.info('Creating Neuromag info structure ...')
    if 'sample_period' in bti_info.keys():
        sfreq = 1. / bti_info['sample_period']
    else:
        sfreq = None

    if pdf_fname is not None:
        info = _empty_info(sfreq)
        date = bti_info['processes'][0]['timestamp']
        info['meas_date'] = [date, 0]
    else:  # these cannot be guessed from config, see docstring
        info = _empty_info(1.0)
        info['sfreq'] = None
        info['lowpass'] = None
        info['highpass'] = None
        info['meas_date'] = None
        bti_info['processes'] = list()

    info['buffer_size_sec'] = 1.  # reasonable default for writing
    # browse processing info for filter specs.
    hp, lp = ((0.0, info['sfreq'] * 0.4) if pdf_fname is not None else
              (None, None))
    hp, lp = info['highpass'], info['lowpass']
    for proc in bti_info['processes']:
        if 'filt' in proc['process_type']:
            for step in proc['processing_steps']:
                if 'high_freq' in step:
                    hp, lp = step['high_freq'], step['low_freq']
                elif 'hp' in step['process_type']:
                    hp = step['freq']
                elif 'lp' in step['process_type']:
                    lp = step['freq']

    info['highpass'] = hp
    info['lowpass'] = lp
    chs = []

    # Note that 'name' and 'chan_label' are not the same.
    # We want the configured label if out IO parsed it
    # except for the MEG channels for which we keep the config name
    bti_ch_names = list()
    for ch in bti_info['chs']:
        # we have always relied on 'A' as indicator of MEG data channels.
        ch_name = ch['name']
        if not ch_name.startswith('A'):
            ch_name = ch.get('chan_label', ch_name)
        bti_ch_names.append(ch_name)

    neuromag_ch_names = _rename_channels(
        bti_ch_names, ecg_ch=ecg_ch, eog_ch=eog_ch)
    ch_mapping = zip(bti_ch_names, neuromag_ch_names)

    logger.info('... Setting channel info structure.')
    for idx, (chan_4d, chan_neuromag) in enumerate(ch_mapping):
        chan_info = dict(zip(FIFF_INFO_CHS_FIELDS, FIFF_INFO_CHS_DEFAULTS))
        chan_info['ch_name'] = chan_neuromag if rename_channels else chan_4d
        chan_info['logno'] = idx + BTI.FIFF_LOGNO
        chan_info['scanno'] = idx + 1
        chan_info['cal'] = bti_info['chs'][idx]['scale']

        if any(chan_4d.startswith(k) for k in ('A', 'M', 'G')):
            loc = bti_info['chs'][idx]['loc']
            if loc is not None:
                if convert:
                    if idx == 0:
                        logger.info('... putting coil transforms in Neuromag '
                                    'coordinates')
                    t = _loc_to_coil_trans(bti_info['chs'][idx]['loc'])
                    t = _convert_coil_trans(t, dev_ctf_t, bti_dev_t)
                    loc = _coil_trans_to_loc(t)
            chan_info['loc'] = loc

        # BTI sensors are natively stored in 4D head coords we believe
        meg_frame = (FIFF.FIFFV_COORD_DEVICE if convert else
                     FIFF.FIFFV_MNE_COORD_4D_HEAD)
        eeg_frame = (FIFF.FIFFV_COORD_HEAD if convert else
                     FIFF.FIFFV_MNE_COORD_4D_HEAD)
        if chan_4d.startswith('A'):
            chan_info['kind'] = FIFF.FIFFV_MEG_CH
            chan_info['coil_type'] = FIFF.FIFFV_COIL_MAGNES_MAG
            chan_info['coord_frame'] = meg_frame
            chan_info['unit'] = FIFF.FIFF_UNIT_T

        elif chan_4d.startswith('M'):
            chan_info['kind'] = FIFF.FIFFV_REF_MEG_CH
            chan_info['coil_type'] = FIFF.FIFFV_COIL_MAGNES_R_MAG
            chan_info['coord_frame'] = meg_frame
            chan_info['unit'] = FIFF.FIFF_UNIT_T

        elif chan_4d.startswith('G'):
            chan_info['kind'] = FIFF.FIFFV_REF_MEG_CH
            chan_info['coord_frame'] = meg_frame
            chan_info['unit'] = FIFF.FIFF_UNIT_T_M
            if chan_4d in ('GxxA', 'GyyA'):
                chan_info['coil_type'] = FIFF.FIFFV_COIL_MAGNES_R_GRAD_DIA
            elif chan_4d in ('GyxA', 'GzxA', 'GzyA'):
                chan_info['coil_type'] = FIFF.FIFFV_COIL_MAGNES_R_GRAD_OFF

        elif chan_4d.startswith('EEG'):
            chan_info['kind'] = FIFF.FIFFV_EEG_CH
            chan_info['coil_type'] = FIFF.FIFFV_COIL_EEG
            chan_info['coord_frame'] = eeg_frame
            chan_info['unit'] = FIFF.FIFF_UNIT_V

        elif chan_4d == 'RESPONSE':
            chan_info['kind'] = FIFF.FIFFV_RESP_CH
        elif chan_4d == 'TRIGGER':
            chan_info['kind'] = FIFF.FIFFV_STIM_CH
        elif chan_4d.startswith('EOG'):
            chan_info['kind'] = FIFF.FIFFV_EOG_CH
        elif chan_4d == ecg_ch:
            chan_info['kind'] = FIFF.FIFFV_ECG_CH
        elif chan_4d.startswith('X'):
            chan_info['kind'] = FIFF.FIFFV_MISC_CH
        elif chan_4d == 'UACurrent':
            chan_info['kind'] = FIFF.FIFFV_MISC_CH

        chs.append(chan_info)

    info['chs'] = chs

    if head_shape_fname:
        logger.info('... Reading digitization points from %s' %
                    head_shape_fname)
        if convert:
            logger.info('... putting digitization points in Neuromag c'
                        'oordinates')
        info['dig'], ctf_head_t = _process_bti_headshape(
            head_shape_fname, convert=convert, use_hpi=use_hpi)

        logger.info('... Computing new device to head transform.')
        # DEV->CTF_DEV->CTF_HEAD->HEAD
        if convert:
            t = combine_transforms(invert_transform(bti_dev_t), dev_ctf_t,
                                   'meg', 'ctf_head')
            dev_head_t = combine_transforms(t, ctf_head_t, 'meg', 'head')
        else:
            dev_head_t = Transform('meg', 'head', np.eye(4))
        logger.info('Done.')
    else:
        logger.info('... no headshape file supplied, doing nothing.')
        dev_head_t = Transform('meg', 'head', np.eye(4))
        ctf_head_t = Transform('ctf_head', 'head', np.eye(4))
    info.update(dev_head_t=dev_head_t, dev_ctf_t=dev_ctf_t,
                ctf_head_t=ctf_head_t)

    if False:  # XXX : reminds us to support this as we go
        # include digital weights from reference channel
        comps = info['comps'] = list()
        weights = bti_info['weights']

        def by_name(x):
            return x[1]
        chn = dict(ch_mapping)
        columns = [chn[k] for k in weights['dsp_ch_names']]
        rows = [chn[k] for k in weights['ch_names']]
        col_order, col_names = zip(*sorted(enumerate(columns),
                                           key=by_name))
        row_order, row_names = zip(*sorted(enumerate(rows), key=by_name))
        # for some reason the C code would invert the signs, so we follow.
        mat = -weights['dsp_wts'][row_order, :][:, col_order]
        comp_data = dict(data=mat,
                         col_names=col_names,
                         row_names=row_names,
                         nrow=mat.shape[0], ncol=mat.shape[1])
        comps += [dict(data=comp_data, ctfkind=101,
                       #  no idea how to calibrate, just ones.
                       rowcals=np.ones(mat.shape[0], dtype='>f4'),
                       colcals=np.ones(mat.shape[1], dtype='>f4'),
                       save_calibrated=0)]
    else:
        logger.info(
            'Currently direct inclusion of 4D weight tables is not supported.'
            ' For critical use cases please take into account the MNE command'
            ' "mne_create_comp_data" to include weights as printed out by '
            'the 4D "print_table" routine.')

    # check that the info is complete
    info._update_redundant()
    info._check_consistency()
    return info, bti_info


@verbose
def read_raw_bti(pdf_fname, config_fname='config',
                 head_shape_fname='hs_file', rotation_x=0.,
                 translation=(0.0, 0.02, 0.11), convert=True,
                 rename_channels=True, sort_by_ch_name=True,
                 ecg_ch='E31', eog_ch=('E63', 'E64'), preload=False,
                 verbose=None):
    """ Raw object from 4D Neuroimaging MagnesWH3600 data

    .. note::
        1. Currently direct inclusion of reference channel weights
           is not supported. Please use ``mne_create_comp_data`` to include
           the weights or use the low level functions from this module to
           include them by yourself.
        2. The informed guess for the 4D name is E31 for the ECG channel and
           E63, E63 for the EOG channels. Pleas check and adjust if those
           channels are present in your dataset but 'ECG 01' and 'EOG 01',
           'EOG 02' don't appear in the channel names of the raw object.

    Parameters
    ----------
    pdf_fname : str
        Path to the processed data file (PDF).
    config_fname : str
        Path to system config file.
    head_shape_fname : str | None
        Path to the head shape file.
    rotation_x : float
        Degrees to tilt x-axis for sensor frame misalignment. Ignored
        if convert is True.
    translation : array-like, shape (3,)
        The translation to place the origin of coordinate system
        to the center of the head. Ignored if convert is True.
    convert : bool
        Convert to Neuromag coordinates or not.
    rename_channels : bool
        Whether to keep original 4D channel labels or not. Defaults to True.
    sort_by_ch_name : bool
        Reorder channels according to channel label. 4D channels don't have
        monotonically increasing numbers in their labels. Defaults to True.
    ecg_ch : str | None
        The 4D name of the ECG channel. If None, the channel will be treated
        as regular EEG channel.
    eog_ch : tuple of str | None
        The 4D names of the EOG channels. If None, the channels will be treated
        as regular EEG channels.
    preload : bool or str (default False)
        Preload data into memory for data manipulation and faster indexing.
        If True, the data will be preloaded into memory (fast, requires
        large amount of memory). If preload is a string, preload is the
        file name of a memory-mapped file which is used to store the data
        on the hard drive (slower, requires less memory).

        ..versionadded:: 0.11

    verbose : bool, str, int, or None
        If not None, override default verbose level (see mne.verbose).

    Returns
    -------
    raw : instance of RawBTi
        A Raw object containing BTI data.

    See Also
    --------
    mne.io.Raw : Documentation of attribute and methods.
    """
    return RawBTi(pdf_fname, config_fname=config_fname,
                  head_shape_fname=head_shape_fname,
                  rotation_x=rotation_x, translation=translation,
                  convert=convert, rename_channels=rename_channels,
                  sort_by_ch_name=sort_by_ch_name, ecg_ch=ecg_ch,
                  eog_ch=eog_ch, preload=preload, verbose=verbose)
