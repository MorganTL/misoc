from migen import *
from migen.genlib.record import *
from migen.genlib import fifo


def _make_m2s(layout):
    r = []
    for f in layout:
        if isinstance(f[1], (int, tuple)):
            r.append((f[0], f[1], DIR_M_TO_S))
        else:
            r.append((f[0], _make_m2s(f[1])))
    return r


class EndpointDescription:
    def __init__(self, payload_layout):
        self.payload_layout = payload_layout

    def get_full_layout(self):
        reserved = {"stb", "ack", "payload", "last", "eop", "description"}
        attributed = set()
        for f in self.payload_layout:
            if f[0] in attributed:
                raise ValueError(f[0] + " already attributed in payload layout")
            if f[0] in reserved:
                raise ValueError(f[0] + " cannot be used in endpoint layout")
            attributed.add(f[0])

        full_layout = [
            ("stb", 1, DIR_M_TO_S),
            ("ack", 1, DIR_S_TO_M),
            ("last", 1, DIR_M_TO_S),
            ("eop", 1, DIR_M_TO_S),
            ("payload", _make_m2s(self.payload_layout))
        ]
        return full_layout


class Endpoint(Record):
    def __init__(self, description_or_layout):
        if isinstance(description_or_layout, EndpointDescription):
            self.description = description_or_layout
        else:
            self.description = EndpointDescription(description_or_layout)
        Record.__init__(self, self.description.get_full_layout())

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "payload"), name)


class _FIFOWrapper(Module):
    def __init__(self, fifo_class, layout, depth, hi_wm=None, lo_wm=None):
        self.sink = Endpoint(layout)
        self.source = Endpoint(layout)

        # # #

        description = self.sink.description
        fifo_layout = [("payload", description.payload_layout), ("eop", 1)]

        watermark_args = {}
        if hi_wm is not None:
            watermark_args["hi_wm"] = hi_wm
        if lo_wm is not None:
            watermark_args["lo_wm"] = lo_wm

        self.submodules.fifo = fifo_class(layout_len(fifo_layout), depth, **watermark_args)
        fifo_in = Record(fifo_layout)
        fifo_out = Record(fifo_layout)
        self.comb += [
            self.fifo.din.eq(fifo_in.raw_bits()),
            fifo_out.raw_bits().eq(self.fifo.dout)
        ]

        self.comb += [
            self.sink.ack.eq(self.fifo.writable),
            self.fifo.we.eq(self.sink.stb),
            fifo_in.eop.eq(self.sink.eop),
            fifo_in.payload.eq(self.sink.payload),

            self.source.stb.eq(self.fifo.readable),
            self.source.eop.eq(fifo_out.eop),
            self.source.payload.eq(fifo_out.payload),
            self.fifo.re.eq(self.source.ack)
        ]

        # Burst transfer with the use of watermarks:
        # FIFO would expect complete bursts to be written to/read from, when
        # given the corresponding watermark arguments, unless the burst is
        # prematurely terminated by asserting sink.eop.
        # A complete burst is a continuous lo_wm/hi_wm of words written to/
        # read from the FIFO.

        # With high watermark, FIFO mandates hi_wm length burst read.
        #
        # source.stb is held low if no complete burst/buffered packet could
        # be transferred.
        # source.last is driven high to signal end of burst.
        if hi_wm is not None:
            transfer_count = Signal(max=hi_wm, reset=hi_wm-1)
            transfer_count_ce = Signal()
            transfer_count_rst = Signal()
            activated = Signal()
            eop_count = Signal(max=depth+1)
            eop_count_next = Signal(max=depth+1)
            has_pending_eop = Signal()

            # helper signals
            do_write = Signal()
            do_read = Signal()

            self.sync += [
                If(transfer_count_rst,
                    transfer_count.eq(transfer_count.reset),
                ).Elif(transfer_count_ce,
                    transfer_count.eq(transfer_count - 1),
                ),
                eop_count.eq(eop_count_next),
            ]

            self.comb += [
                # Avoid downstream overreading
                self.fifo.re.eq(self.source.stb & self.source.ack),

                do_write.eq(self.fifo.we & self.fifo.writable),
                do_read.eq(self.fifo.re & self.fifo.readable),
                has_pending_eop.eq(eop_count_next != 0),

                eop_count_next.eq(eop_count),

                If(fifo_in.eop & do_write,
                    If(~(fifo_out.eop & do_read),
                        eop_count_next.eq(eop_count + 1),
                    ),
                ).Elif(fifo_out.eop & do_read,
                    eop_count_next.eq(eop_count - 1),
                ),
            ]

            # Stream control
            self.comb += [
                self.source.last.eq((transfer_count == 0) | fifo_out.eop),
                self.source.stb.eq(self.fifo.readable & (self.fifo.almost_full | activated)),
                transfer_count_ce.eq(do_read),
                transfer_count_rst.eq(do_read & self.source.last),
            ]

            self.sync += [
                If(~activated,
                    activated.eq(self.fifo.almost_full | (self.sink.eop & do_write))
                ).Elif(do_read & self.source.last,
                    activated.eq(has_pending_eop),
                ),
            ]

        # With low watermark, FIFO accepts lo_wm length burst write.
        #
        # sink.last must indicate the end of burst
        #
        # It is the upstream's duty to drive sink.last signal appropriately.
        if lo_wm is not None:
            recv_activated = Signal()

            # helper signals
            do_write = Signal()

            self.comb += [
                do_write.eq(self.fifo.we & self.fifo.writable),

                # Avoid upstream overwriting
                self.fifo.we.eq(self.sink.stb & self.sink.ack),
            ]

            # recv stream control
            self.comb += \
                self.sink.ack.eq(self.fifo.writable & (
                    self.fifo.almost_empty  # Can accept long burst
                    | recv_activated        # In the middle of a burst
                ))

            self.sync += \
                If(~recv_activated,
                    # Avoid entry to burst state if it is a 1 word burst
                    recv_activated.eq(self.fifo.almost_empty & ~(do_write & self.sink.eop)),
                ).Elif(recv_activated & (do_write & (self.sink.last | self.sink.eop)),
                    # almost_empty needs 1 cycle to update
                    recv_activated.eq(0),
                )


class SyncFIFO(_FIFOWrapper):
    def __init__(self, layout, depth, buffered=False, hi_wm=None, lo_wm=None):
        _FIFOWrapper.__init__(
            self,
            fifo.SyncFIFOBuffered if buffered else fifo.SyncFIFO,
            layout, depth, hi_wm, lo_wm)


class AsyncFIFO(_FIFOWrapper):
    def __init__(self, layout, depth):
        _FIFOWrapper.__init__(self, fifo.AsyncFIFO, layout, depth)


class Multiplexer(Module):
    def __init__(self, layout, n):
        self.source = Endpoint(layout)
        sinks = []
        for i in range(n):
            sink = Endpoint(layout)
            setattr(self, "sink"+str(i), sink)
            sinks.append(sink)
        self.sel = Signal(max=n)

        # # #

        cases = {}
        for i, sink in enumerate(sinks):
            cases[i] = sink.connect(self.source)
        self.comb += Case(self.sel, cases)


class Demultiplexer(Module):
    def __init__(self, layout, n):
        self.sink = Endpoint(layout)
        sources = []
        for i in range(n):
            source = Endpoint(layout)
            setattr(self, "source"+str(i), source)
            sources.append(source)
        self.sel = Signal(max=n)

        # # #

        cases = {}
        for i, source in enumerate(sources):
            cases[i] = self.sink.connect(source)
        self.comb += Case(self.sel, cases)


class _UpConverter(Module):
    def __init__(self, nbits_from, nbits_to, ratio, reverse,
                 report_valid_token_count):
        self.sink = sink = Endpoint([("data", nbits_from)])
        source_layout = [("data", nbits_to)]
        if report_valid_token_count:
            source_layout.append(("valid_token_count", bits_for(ratio)))
        self.source = source = Endpoint(source_layout)
        self.ratio = ratio

        # # #

        # control path
        demux = Signal(max=ratio)
        load_part = Signal()
        strobe_all = Signal()
        self.comb += [
            sink.ack.eq(~strobe_all | source.ack),
            source.stb.eq(strobe_all),
            load_part.eq(sink.stb & sink.ack),
            # cannot burst
            source.last.eq(1)
        ]

        demux_last = ((demux == (ratio - 1)) | sink.eop)

        self.sync += [
            If(source.ack, strobe_all.eq(0)),
            If(load_part,
                If(demux_last,
                    demux.eq(0),
                    strobe_all.eq(1)
                ).Else(
                    demux.eq(demux + 1)
                )
            ),
            If(source.stb & source.ack,
                source.eop.eq(sink.eop),
            ).Elif(sink.stb & sink.ack,
                source.eop.eq(sink.eop | source.eop)
            )
        ]

        # data path
        cases = {}
        for i in range(ratio):
            n = ratio-i-1 if reverse else i
            cases[i] = source.data[n*nbits_from:(n+1)*nbits_from].eq(sink.data)
        self.sync += If(load_part, Case(demux, cases))

        if report_valid_token_count:
            self.sync += If(load_part, source.valid_token_count.eq(demux + 1))


class _DownConverter(Module):
    def __init__(self, nbits_from, nbits_to, ratio, reverse,
                 report_valid_token_count):
        self.sink = sink = Endpoint([("data", nbits_from)])
        source_layout = [("data", nbits_to)]
        if report_valid_token_count:
            source_layout.append(("valid_token_count", 1))
        self.source = source = Endpoint(source_layout)
        self.ratio = ratio

        # # #

        # control path
        mux = Signal(max=ratio)
        last = Signal()
        self.comb += [
            last.eq(mux == (ratio-1)),
            source.stb.eq(sink.stb),
            source.eop.eq(sink.eop & last),
            source.last.eq(sink.last & last),
            sink.ack.eq(last & source.ack)
        ]
        self.sync += \
            If(source.stb & source.ack,
                If(last,
                    mux.eq(0)
                ).Else(
                    mux.eq(mux + 1)
                )
            )

        # data path
        cases = {}
        for i in range(ratio):
            n = ratio-i-1 if reverse else i
            cases[i] = source.data.eq(sink.data[n*nbits_to:(n+1)*nbits_to])
        self.comb += Case(mux, cases).makedefault()

        if report_valid_token_count:
            self.comb += source.valid_token_count.eq(last)


class _IdentityConverter(Module):
    def __init__(self, nbits_from, nbits_to, ratio, reverse,
                 report_valid_token_count):
        self.sink = sink = Endpoint([("data", nbits_from)])
        source_layout = [("data", nbits_to)]
        if report_valid_token_count:
            source_layout.append(("valid_token_count", 1))
        self.source = source = Endpoint(source_layout)
        assert ratio == 1
        self.ratio = ratio

        # # #

        self.comb += sink.connect(source)
        if report_valid_token_count:
            self.comb += source.valid_token_count.eq(1)


def _get_converter_ratio(nbits_from, nbits_to):
    if nbits_from > nbits_to:
        specialized_cls = _DownConverter
        if nbits_from % nbits_to:
            raise ValueError("Ratio must be an int")
        ratio = nbits_from//nbits_to
    elif nbits_from < nbits_to:
        specialized_cls = _UpConverter
        if nbits_to % nbits_from:
            raise ValueError("Ratio must be an int")
        ratio = nbits_to//nbits_from
    else:
        specialized_cls = _IdentityConverter
        ratio = 1

    return specialized_cls, ratio


class Converter(Module):
    def __init__(self, nbits_from, nbits_to, reverse=False,
                 report_valid_token_count=False):
        cls, ratio = _get_converter_ratio(nbits_from, nbits_to)
        self.submodules.specialized = cls(nbits_from, nbits_to, ratio,
                                          reverse, report_valid_token_count)
        self.sink = self.specialized.sink
        self.source = self.specialized.source


class StrideConverter(Module):
    def __init__(self, layout_from, layout_to, *args, **kwargs):
        self.sink = sink = Endpoint(layout_from)
        self.source = source = Endpoint(layout_to)

        # # #

        nbits_from = len(sink.payload.raw_bits())
        nbits_to = len(source.payload.raw_bits())

        converter = Converter(nbits_from, nbits_to, *args, **kwargs)
        self.submodules += converter

        # cast sink to converter.sink (user fields --> raw bits)
        self.comb += [
            converter.sink.stb.eq(sink.stb),
            converter.sink.last.eq(sink.last),
            converter.sink.eop.eq(sink.eop),
            sink.ack.eq(converter.sink.ack)
        ]
        if isinstance(converter.specialized, _DownConverter):
            ratio = converter.specialized.ratio
            for i in range(ratio):
                j = 0
                for name, width in layout_to:
                    src = getattr(sink, name)[i*width:(i+1)*width]
                    dst = converter.sink.data[i*nbits_to+j:i*nbits_to+j+width]
                    self.comb += dst.eq(src)
                    j += width
        else:
            self.comb += converter.sink.data.eq(sink.payload.raw_bits())


        # cast converter.source to source (raw bits --> user fields)
        self.comb += [
            source.stb.eq(converter.source.stb),
            source.last.eq(converter.source.last),
            source.eop.eq(converter.source.eop),
            converter.source.ack.eq(source.ack)
        ]
        if isinstance(converter.specialized, _UpConverter):
            ratio = converter.specialized.ratio
            for i in range(ratio):
                j = 0
                for name, width in layout_from:
                    src = converter.source.data[i*nbits_from+j:i*nbits_from+j+width]
                    dst = getattr(source, name)[i*width:(i+1)*width]
                    self.comb += dst.eq(src)
                    j += width
        else:
            self.comb += source.payload.raw_bits().eq(converter.source.data)


class Buffer(Module):
    def __init__(self, layout):
        self.sink = Endpoint(layout)
        self.source = Endpoint(layout)

        # # #

        self.sync += \
            If(self.source.ack,
                self.sink.connect(self.source, omit={"ack"}),
            ),

        self.comb += self.sink.ack.eq(self.source.ack),
