'''
Copyright (C) 2023 CG Cookie
http://cgcookie.com
hello@cgcookie.com

Created by Jonathan Denning, Jonathan Williamson, and Patrick Moore

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.
'''

import random

from mathutils.geometry import intersect_line_line_2d as intersect2d_segment_segment

from ..rftool import RFTool
from ..rfwidgets.rfwidget_default import RFWidget_Default_Factory
from ..rfwidgets.rfwidget_hidden  import RFWidget_Hidden_Factory
from ..rfmesh.rfmesh_wrapper import RFVert, RFEdge, RFFace

from ...addon_common.common import gpustate
from ...addon_common.common.drawing import (
    CC_DRAW,
    CC_2D_POINTS,
    CC_2D_LINES, CC_2D_LINE_LOOP,
    CC_2D_TRIANGLES, CC_2D_TRIANGLE_FAN,
)
from ...addon_common.common.profiler import profiler
from ...addon_common.common.maths import Point, Point2D, Vec2D, Vec, Direction2D, intersection2d_line_line, closest2d_point_segment
from ...addon_common.common.fsm import FSM
from ...addon_common.common.globals import Globals
from ...addon_common.common.utils import iter_pairs
from ...addon_common.common.blender import tag_redraw_all
from ...addon_common.common.drawing import DrawCallbacks
from ...addon_common.common.boundvar import BoundBool, BoundInt, BoundFloat, BoundString
from ...addon_common.common.timerhandler import CallGovernor
from ...addon_common.common.debug import dprint


from ...config.options import options, themes


class PolyPen_Insert():
    @RFTool.on_events('target change')
    @FSM.onlyinstate('previs insert')
    @RFTool.not_while_navigating
    def gather_selection(self):
        self.sel_verts, self.sel_edges, self.sel_faces = self.rfcontext.get_selected_geom()
        self.num_sel_verts, self.num_sel_edges, self.num_sel_faces = len(self.sel_verts), len(self.sel_edges), len(self.sel_faces)

    @RFTool.on_events('target change', 'view change')
    @FSM.onlyinstate('previs insert')
    @RFTool.not_while_navigating
    def gather_visible(self):
        self.vis_verts, self.vis_edges, self.vis_faces = self.rfcontext.get_vis_geom()

    @FSM.on_state('previs insert', 'enter')
    def modal_previs_enter(self):
        self.draw_coords = []
        self.gather_visible()
        self.gather_selection()
        self.set_next_state()
        self.rfcontext.fast_update_timer.enable(True)
        self.modal_previs_mousemove()
        tag_redraw_all('PolyPen insert mouse move')

    @RFTool.on_mouse_move
    @FSM.onlyinstate('previs insert')
    def modal_previs_mousemove(self):
        if self.next_state == 'knife selected edge':
            self.set_widget('knife')
        else:
            self.set_widget('insert')

    @FSM.on_state('previs insert')
    def modal_previs(self):
        if self.handle_inactive_passthrough(): return

        if self.actions.pressed('insert'):
            return 'insert'

        if not self.actions.using_onlymods('insert'):
            return 'main'


    @FSM.on_state('previs insert', 'exit')
    def modal_previs_exit(self):
        self.rfcontext.fast_update_timer.enable(False)


    @DrawCallbacks.on_draw('post2d')
    @FSM.onlyinstate('previs insert')
    @RFTool.not_while_navigating
    def draw_postpixel(self):
        gpustate.blend('ALPHA')
        CC_DRAW.stipple(pattern=[4,4])
        CC_DRAW.point_size(8)
        CC_DRAW.line_width(2)

        poly_alpha = 0.2
        line_color = themes['new']
        poly_color = [line_color[0], line_color[1], line_color[2], line_color[3] * poly_alpha]

        for coords in self.draw_coords:
            coords = [self.rfcontext.Point_to_Point2D(co) for co in coords]
            if not all(coords): return

            match len(coords):
                case 1:
                    with Globals.drawing.draw(CC_2D_POINTS) as draw:
                        draw.color(line_color)
                        for c in coords:
                            draw.vertex(c)
                case 2:
                    with Globals.drawing.draw(CC_2D_LINES) as draw:
                        draw.color(line_color)
                        draw.vertex(coords[0])
                        draw.vertex(coords[1])
                case _:
                    with Globals.drawing.draw(CC_2D_LINE_LOOP) as draw:
                        draw.color(line_color)
                        for co in coords: draw.vertex(co)

                    with Globals.drawing.draw(CC_2D_TRIANGLE_FAN) as draw:
                        draw.color(poly_color)
                        draw.vertex(coords[0])
                        for co1,co2 in iter_pairs(coords[1:], False):
                            draw.vertex(co1)
                            draw.vertex(co2)

        CC_DRAW.stipple()


    @FSM.on_state('insert')
    def insert(self):
        self.rfcontext.undo_push('insert')
        return self._insert()


    @RFTool.on_mouse_move
    @RFTool.once_per_frame
    # @RFTool.on_events('new frame')
    @RFTool.not_while_navigating
    @FSM.onlyinstate('previs insert')
    def set_next_state(self):
        '''
        determines what the next state will be, based on selected mode, selected geometry, and hovered geometry
        '''

        self.draw_coords = []
        self.nearest_vert, self.nearest_edge, self.nearest_face, self.nearest_geom = None, None, None, None
        self.insert_edge = None

        if not self.actions.mouse: return
        hit_pos = self.actions.hit_pos
        if not hit_pos: return

        with profiler.code('getting nearest geometry'):
            self.nearest_vert,_ = self.rfcontext.accel_nearest2D_vert(max_dist=options['polypen merge dist'])
            self.nearest_edge,_ = self.rfcontext.accel_nearest2D_edge(max_dist=options['polypen merge dist'])
            self.nearest_face,_ = self.rfcontext.accel_nearest2D_face(max_dist=options['polypen merge dist'])
            self.nearest_geom = self.nearest_vert or self.nearest_edge or self.nearest_face
            self.insert_edge,_ = self.rfcontext.accel_nearest2D_edge(max_dist=options['polypen insert dist'])

        if self.insert_edge and self.insert_edge.select:      # overriding: if hovering over a selected edge, knife it!
            self.next_state = 'knife selected edge'

        elif options['polypen insert mode'] == 'Tri/Quad':
            if self.num_sel_verts == 1 and self.num_sel_edges == 0 and self.num_sel_faces == 0:
                self.next_state = 'vert-edge'
            elif self.num_sel_edges and self.num_sel_faces == 0:
                quad_snap = (
                    (not self.nearest_vert and self.nearest_edge) and
                    (len(self.nearest_edge.link_faces) <= 1) and
                    (not any(v in self.sel_verts for v in self.nearest_edge.verts)) and
                    (not any(e in f.edges for v in self.nearest_edge.verts for f in v.link_faces for e in self.sel_edges))
                )
                if quad_snap:
                    self.next_state = 'edge-quad-snap'
                else:
                    self.next_state = 'edge-face'
            elif self.num_sel_verts == 3 and self.num_sel_edges == 3 and self.num_sel_faces == 1:
                self.next_state = 'tri-quad'
            else:
                self.next_state = 'new vertex'

        elif options['polypen insert mode'] == 'Quad-Only':
            # a Desmos construction of how this works: https://www.desmos.com/geometry/bmmx206thi
            if self.num_sel_verts == 1 and self.num_sel_edges == 0 and self.num_sel_faces == 0:
                self.next_state = 'vert-edge'
            elif self.num_sel_edges:
                quad_snap = (
                    (not self.nearest_vert and self.nearest_edge) and
                    (len(self.nearest_edge.link_faces) <= 1) and
                    (not any(v in self.sel_verts for v in self.nearest_edge.verts)) and
                    (not any(e in f.edges for v in self.nearest_edge.verts for f in v.link_faces for e in self.sel_edges))
                )
                self.next_state = 'edge-quad-snap' if quad_snap else 'edge-quad'
            else:
                self.next_state = 'new vertex'

        elif options['polypen insert mode'] == 'Tri-Only':
            if self.num_sel_verts == 1 and self.num_sel_edges == 0 and self.num_sel_faces == 0:
                self.next_state = 'vert-edge'
            elif self.num_sel_edges and self.num_sel_faces == 0:
                quad = (
                    (not self.nearest_vert and self.nearest_edge) and
                    (len(self.nearest_edge.link_faces) <= 1) and
                    (not any(v in self.sel_verts for v in self.nearest_edge.verts)) and
                    (not any(e in f.edges for v in self.nearest_edge.verts for f in v.link_faces for e in self.sel_edges))
                )
                if quad:
                    self.next_state = 'edge-quad-snap'
                else:
                    self.next_state = 'edge-face'
            elif self.num_sel_verts == 3 and self.num_sel_edges == 3 and self.num_sel_faces == 1:
                self.next_state = 'edge-face'
            else:
                self.next_state = 'new vertex'

        elif options['polypen insert mode'] == 'Edge-Only':
            if self.num_sel_verts == 0:
                self.next_state = 'new vertex'
            else:
                if self.insert_edge:
                    self.next_state = 'vert-edge'
                else:
                    self.next_state = 'vert-edge-vert'

        else:
            assert False, f'Unhandled PolyPen insert mode: {options["polypen insert mode"]}'

        tag_redraw_all('PolyPen next state')

        match self.next_state:
            case 'unset':
                return

            case 'knife selected edge':
                bmv1,bmv2 = self.insert_edge.verts
                faces = self.insert_edge.link_faces
                if faces:
                    for f in faces:
                        lco = []
                        for v0,v1 in iter_pairs(f.verts, True):
                            lco.append(v0.co)
                            if (v0 == bmv1 and v1 == bmv2) or (v0 == bmv2 and v1 == bmv1):
                                lco.append(hit_pos)
                        self.draw_coords.append(lco)
                else:
                    self.draw_coords.append([bmv1.co, hit_pos])
                    self.draw_coords.append([bmv2.co, hit_pos])
                return

            case 'new vertex':
                p0 = hit_pos
                if self.insert_edge:
                    bmv1,bmv2 = self.insert_edge.verts
                    if f := next(iter(self.insert_edge.link_faces), None):
                        lco = []
                        for v0,v1 in iter_pairs(f.verts, True):
                            lco.append(v0.co)
                            if (v0 == bmv1 and v1 == bmv2) or (v0 == bmv2 and v1 == bmv1):
                                lco.append(p0)
                        self.draw_coords.append(lco)
                    else:
                        self.draw_coords.append([bmv1.co, hit_pos])
                        self.draw_coords.append([bmv2.co, hit_pos])
                else:
                    self.draw_coords.append([hit_pos])
                return

            case 'vert-edge' | 'vert-edge-vert':
                bmv0,_ = self.rfcontext.nearest2D_vert(verts=self.sel_verts)
                if self.nearest_vert:
                    p0 = self.nearest_vert.co
                elif self.next_state == 'vert-edge':
                    p0 = hit_pos
                    if self.insert_edge:
                        bmv1,bmv2 = self.insert_edge.verts
                        if f := next(iter(self.insert_edge.link_faces), None):
                            lco = []
                            for v0,v1 in iter_pairs(f.verts, True):
                                lco.append(v0.co)
                                if (v0 == bmv1 and v1 == bmv2) or (v0 == bmv2 and v1 == bmv1):
                                    lco.append(p0)
                            self.draw_coords.append(lco)
                        else:
                            self.draw_coords.append([bmv1.co, p0])
                            self.draw_coords.append([bmv2.co, p0])
                elif self.next_state == 'vert-edge-vert':
                    p0 = hit_pos
                else:
                    return
                if bmv0: self.draw_coords.append([bmv0.co, p0])
                return

            case 'edge-face':
                e0,_ = self.rfcontext.nearest2D_edge(edges=self.sel_edges)
                e1 = self.insert_edge
                if not e0: return
                if e1 and e0 == e1:
                    bmv1,bmv2 = e1.verts
                    p0 = hit_pos
                    f = next(iter(e1.link_faces), None)
                    if f:
                        lco = []
                        for v0,v1 in iter_pairs(f.verts, True):
                            lco.append(v0.co)
                            if (v0 == bmv1 and v1 == bmv2) or (v0 == bmv2 and v1 == bmv1):
                                lco.append(p0)
                        self.draw_coords.append(lco)
                    else:
                        self.draw_coords.append([bmv1.co, hit_pos])
                        self.draw_coords.append([bmv2.co, hit_pos])
                else:
                    # self.draw_coords.append([hit_pos])
                    bmv1,bmv2 = e0.verts
                    if self.nearest_vert and not self.nearest_vert.select:
                        p0 = self.nearest_vert.co
                    else:
                        p0 = hit_pos
                    self.draw_coords.append([p0, bmv1.co, bmv2.co])
                return

            case 'edge-quad':
                # a Desmos construction of how this works: https://www.desmos.com/geometry/bmmx206thi
                xy0, xy1, xy2, xy3 = self._get_edge_quad_verts()
                if xy0 is None: return
                co0 = self.rfcontext.raycast_sources_Point2D(xy0)[0]
                co1 = self.rfcontext.raycast_sources_Point2D(xy1)[0]
                co2 = self.rfcontext.raycast_sources_Point2D(xy2)[0]
                co3 = self.rfcontext.raycast_sources_Point2D(xy3)[0]
                self.draw_coords.append([co1, co2, co3, co0])
                return

            case 'edge-quad-snap':
                e0,_ = self.rfcontext.nearest2D_edge(edges=self.sel_edges)
                e1 = self.nearest_edge
                if not e0 or not e1: return
                bmv0,bmv1 = e0.verts
                bmv2,bmv3 = e1.verts
                p0,p1 = self.rfcontext.Point_to_Point2D(bmv0.co),self.rfcontext.Point_to_Point2D(bmv1.co)
                p2,p3 = self.rfcontext.Point_to_Point2D(bmv2.co),self.rfcontext.Point_to_Point2D(bmv3.co)
                if intersect2d_segment_segment(p1, p2, p3, p0): bmv2,bmv3 = bmv3,bmv2
                # if e0.vector2D(self.rfcontext.Point_to_Point2D).dot(e1.vector2D(self.rfcontext.Point_to_Point2D)) > 0:
                #     bmv2,bmv3 = bmv3,bmv2
                self.draw_coords.append([bmv0.co, bmv1.co, bmv2.co, bmv3.co])
                return

            case 'tri-quad':
                if self.nearest_vert and not self.nearest_vert.select:
                    p0 = self.nearest_vert.co
                else:
                    p0 = hit_pos
                e1,_ = self.rfcontext.nearest2D_edge(edges=self.sel_edges)
                if not e1: return
                bmv1,bmv2 = e1.verts
                f = next(iter(e1.link_faces), None)
                if not f: return
                lco = []
                for v0,v1 in iter_pairs(f.verts, True):
                    lco.append(v0.co)
                    if (v0 == bmv1 and v1 == bmv2) or (v0 == bmv2 and v1 == bmv1):
                        lco.append(p0)
                self.draw_coords.append(lco)
                #self.draw_coords.append([p0, bmv1.co, bmv2.co])
                return

            case _:
                pass

        # case 'edges-face':
        #     if self.nearest_vert and not self.nearest_vert.select:
        #         p0 = self.nearest_vert.co
        #     else:
        #         p0 = hit_pos
        #     e1,_ = self.rfcontext.nearest2D_edge(edges=self.sel_edges)
        #     bmv1,bmv2 = e1.verts
        #     self.draw_coords.append([p0, bmv1.co, bmv2.co])

        # if self.actions.shift and not self.actions.ctrl:
        #     # TODO: ALTERNATIVE INSERT, BUT NOT BEING USED!?!?
        #     #       not in docs, not in main polypen.py FSM state
        #     match self.next_state:
        #         case 'edge-face' | 'edge-quad' | 'edge-quad-snap' | 'tri-quad':
        #             nearest_sel_vert,_ = self.rfcontext.nearest2D_vert(verts=self.sel_verts, max_dist=options['polypen merge dist'])
        #             if nearest_sel_vert:
        #                 self.draw_coords.append([nearest_sel_vert.co, hit_pos])
        #             return
        #         case _:
        #             return


    def _get_edge_quad_verts(self):
        '''
        this function is used in quad-only mode to find positions of quad verts based on selected edge and mouse position
        a Desmos construction of how this works: https://www.desmos.com/geometry/5w40xowuig
        '''
        e0,_ = self.rfcontext.nearest2D_edge(edges=self.sel_edges)
        if not e0: return (None, None, None, None)
        bmv0,bmv1 = e0.verts
        xy0 = self.rfcontext.Point_to_Point2D(bmv0.co)
        xy1 = self.rfcontext.Point_to_Point2D(bmv1.co)
        d01 = (xy0 - xy1).length
        mid01 = xy0 + (xy1 - xy0) / 2
        mid23 = self.actions.mouse
        mid0123 = mid01 + (mid23 - mid01) / 2
        between = mid23 - mid01
        if between.length < 0.0001: return (None, None, None, None)
        perp = Direction2D((-between.y, between.x))
        if perp.dot(xy1 - xy0) < 0: perp.reverse()
        #pts = intersect_line_line(xy0, xy1, mid0123, mid0123 + perp)
        #if not pts: return (None, None, None, None)
        #intersection = pts[1]
        intersection = intersection2d_line_line(xy0, xy1, mid0123, mid0123 + perp)
        if not intersection: return (None, None, None, None)
        intersection = Point2D(intersection)

        toward = Direction2D(mid23 - intersection)
        if toward.dot(perp) < 0: d01 = -d01

        # push intersection out just a bit to make it more stable (prevent crossing) when |between| < d01
        between_len = between.length * Direction2D(xy1 - xy0).dot(perp)

        for tries in range(32):
            v = toward * (d01 / 2)
            xy2, xy3 = mid23 + v, mid23 - v

            # try to prevent quad from crossing
            v03 = xy3 - xy0
            if v03.dot(between) < 0 or v03.length < between_len:
                xy3 = xy0 + Direction2D(v03) * (between_len * (-1 if v03.dot(between) < 0 else 1))
            v12 = xy2 - xy1
            if v12.dot(between) < 0 or v12.length < between_len:
                xy2 = xy1 + Direction2D(v12) * (between_len * (-1 if v12.dot(between) < 0 else 1))

            if self.rfcontext.raycast_sources_Point2D(xy2)[0] and self.rfcontext.raycast_sources_Point2D(xy3)[0]: break
            d01 /= 2
        else:
            return (None, None, None, None)

        nearest_vert,_ = self.rfcontext.nearest2D_vert(point=xy2, verts=self.vis_verts, max_dist=options['polypen merge dist'])
        if nearest_vert: xy2 = self.rfcontext.Point_to_Point2D(nearest_vert.co)
        nearest_vert,_ = self.rfcontext.nearest2D_vert(point=xy3, verts=self.vis_verts, max_dist=options['polypen merge dist'])
        if nearest_vert: xy3 = self.rfcontext.Point_to_Point2D(nearest_vert.co)

        return (xy0, xy1, xy2, xy3)

    @RFTool.dirty_when_done
    def _insert(self):
        if self.actions.shift and not self.actions.ctrl and not self.next_state in ['new vertex', 'vert-edge']:
            self.next_state = 'vert-edge'
            nearest_vert,_ = self.rfcontext.nearest2D_vert(verts=self.sel_verts, max_dist=options['polypen merge dist'])
            self.rfcontext.select(nearest_vert)

        sel_verts = self.sel_verts
        sel_edges = self.sel_edges
        sel_faces = self.sel_faces

        if self.next_state == 'knife selected edge':            # overriding: if hovering over a selected edge, knife it!
            # self.nearest_edge and self.nearest_edge.select:
            #print('knifing selected, hovered edge')
            bmv = self.rfcontext.new2D_vert_mouse()
            if not bmv:
                self.rfcontext.undo_cancel()
                return 'main'
            bme0,bmv2 = self.insert_edge.split()
            bmv.merge(bmv2)
            self.rfcontext.select(bmv)
            self.mousedown = self.actions.mousedown
            xy = self.rfcontext.Point_to_Point2D(bmv.co)
            if not xy:
                #print('Could not insert: ' + str(bmv.co))
                self.rfcontext.undo_cancel()
                return 'main'
            self.prep_move(
                bmverts=[bmv],
                action_confirm=(lambda: self.actions.released('insert')),
            )
            return 'move'

        if self.next_state in {'vert-edge', 'vert-edge-vert'}:
            bmv0,_ = self.rfcontext.nearest2D_vert(verts=self.sel_verts)
            if not bmv0:
                self.rfcontext.undo_cancel()
                return 'main'

            if self.next_state == 'vert-edge':
                if self.nearest_vert:
                    bmv1 = self.nearest_vert
                    if bmv0 == bmv1:
                        self.prep_move(
                            bmverts=[bmv0],
                            action_confirm=(lambda: self.actions.released('insert')),
                        )
                        return 'move'
                    lbmf = bmv0.shared_faces(bmv1)
                    bme = bmv0.shared_edge(bmv1)
                    if len(lbmf) == 1 and not bmv0.share_edge(bmv1):
                        # split face
                        bmf = lbmf[0]
                        bmf.split(bmv0, bmv1)
                        self.rfcontext.select(bmv1)
                        return 'main'
                    if not bme:
                        bme = self.rfcontext.new_edge((bmv0, bmv1))
                    self.rfcontext.select(bme)
                    self.prep_move(
                        bmverts=[bmv1],
                        action_confirm=(lambda: self.actions.released('insert')),
                    )
                    return 'move'

                bmv1 = self.rfcontext.new2D_vert_mouse()
                if not bmv1:
                    self.rfcontext.undo_cancel()
                    return 'main'
                if self.nearest_edge:
                    if bmv0 in self.nearest_edge.verts:
                        # selected vert already part of edge; split
                        bme0,bmv2 = self.nearest_edge.split()
                        bmv1.merge(bmv2)
                        self.rfcontext.select(bmv1)
                    else:
                        bme0,bmv2 = self.nearest_edge.split()
                        bmv1.merge(bmv2)
                        bmf = next(iter(bmv0.shared_faces(bmv1)), None)
                        if bmf:
                            if not bmv0.share_edge(bmv1):
                                bmf.split(bmv0, bmv1)
                        if not bmv0.share_face(bmv1):
                            bme = self.rfcontext.new_edge((bmv0, bmv1))
                            self.rfcontext.select(bme)
                        self.rfcontext.select(bmv1)
                else:
                    bme = self.rfcontext.new_edge((bmv0, bmv1))
                    self.rfcontext.select(bme)

            elif self.next_state == 'vert-edge-vert':
                if self.nearest_vert:
                    bmv1 = self.nearest_vert
                else:
                    bmv1 = self.rfcontext.new2D_vert_mouse()
                    if not bmv1:
                        self.rfcontext.undo_cancel()
                        return 'main'
                if bmv0 == bmv1:
                    return 'main'
                bme = bmv0.shared_edge(bmv1) or self.rfcontext.new_edge((bmv0, bmv1))
                self.rfcontext.select(bmv1)

            else:
                return 'main'

            self.mousedown = self.actions.mousedown
            xy = self.rfcontext.Point_to_Point2D(bmv1.co)
            if not xy:
                dprint('Could not insert: ' + str(bmv1.co))
                self.rfcontext.undo_cancel()
                return 'main'
            self.prep_move(
                bmverts=[bmv1],
                action_confirm=(lambda: self.actions.released('insert')),
            )
            return 'move'

        if self.next_state == 'edge-face':
            bme,_ = self.rfcontext.nearest2D_edge(edges=self.sel_edges)
            if not bme: return
            bmv0,bmv1 = bme.verts

            if self.nearest_vert and not self.nearest_vert.select:
                bmv2 = self.nearest_vert
                bmf = self.rfcontext.new_face([bmv0, bmv1, bmv2])
                self.rfcontext.clean_duplicate_bmedges(bmv2)
            else:
                bmv2 = self.rfcontext.new2D_vert_mouse()
                if not bmv2:
                    self.rfcontext.undo_cancel()
                    return 'main'
                bmf = self.rfcontext.new_face([bmv0, bmv1, bmv2])

            if bmf: self.rfcontext.select(bmf)
            self.mousedown = self.actions.mousedown
            xy = self.rfcontext.Point_to_Point2D(bmv2.co)
            if not xy:
                dprint('Could not insert: ' + str(bmv2.co))
                self.rfcontext.undo_cancel()
                return 'main'
            self.prep_move(
                bmverts=[bmv2],
                action_confirm=(lambda: self.actions.released('insert')),
            )
            return 'move'

        if self.next_state == 'edge-quad':
            xy0,xy1,xy2,xy3 = self._get_edge_quad_verts()
            if xy0 is None or xy1 is None or xy2 is None or xy3 is None: return
            # a Desmos construction of how this works: https://www.desmos.com/geometry/bmmx206thi
            e0,_ = self.rfcontext.nearest2D_edge(edges=self.sel_edges)
            if not e0: return
            bmv0,bmv1 = e0.verts

            bmv2,_ = self.rfcontext.nearest2D_vert(point=xy2, verts=self.vis_verts, max_dist=options['polypen merge dist'])
            if not bmv2: bmv2 = self.rfcontext.new2D_vert_point(xy2)
            bmv3,_ = self.rfcontext.nearest2D_vert(point=xy3, verts=self.vis_verts, max_dist=options['polypen merge dist'])
            if not bmv3: bmv3 = self.rfcontext.new2D_vert_point(xy3)
            if not bmv2 or not bmv3:
                self.rfcontext.undo_cancel()
                return 'main'
            e1 = bmv2.shared_edge(bmv3)
            if not e1: e1 = self.rfcontext.new_edge([bmv2, bmv3])
            self.rfcontext.new_face([bmv0, bmv1, bmv2, bmv3])
            bmes = [bmv1.shared_edge(bmv2), bmv0.shared_edge(bmv3), bmv2.shared_edge(bmv3)]
            self.rfcontext.select(bmes, subparts=False)
            self.mousedown = self.actions.mousedown
            self.prep_move(
                bmverts=[bmv2, bmv3],
                action_confirm=(lambda: self.actions.released('insert')),
            )
            return 'move'

        if self.next_state == 'edge-quad-snap':
            e0,_ = self.rfcontext.nearest2D_edge(edges=self.sel_edges)
            e1 = self.nearest_edge
            if not e0 or not e1: return
            bmv0,bmv1 = e0.verts
            bmv2,bmv3 = e1.verts
            p0,p1 = self.rfcontext.Point_to_Point2D(bmv0.co),self.rfcontext.Point_to_Point2D(bmv1.co)
            p2,p3 = self.rfcontext.Point_to_Point2D(bmv2.co),self.rfcontext.Point_to_Point2D(bmv3.co)
            if intersect2d_segment_segment(p1, p2, p3, p0): bmv2,bmv3 = bmv3,bmv2
            # if e0.vector2D(self.rfcontext.Point_to_Point2D).dot(e1.vector2D(self.rfcontext.Point_to_Point2D)) > 0:
            #     bmv2,bmv3 = bmv3,bmv2
            self.rfcontext.new_face([bmv0, bmv1, bmv2, bmv3])
            # select all non-manifold edges that share vertex with e1
            bmes = [e for e in bmv2.link_edges + bmv3.link_edges if not e.is_manifold and not e.share_face(e1)]
            if not bmes:
                bmes = [bmv1.shared_edge(bmv2), bmv0.shared_edge(bmv3)]
            self.rfcontext.select(bmes, subparts=False)
            return 'main'

        if self.next_state == 'tri-quad':
            hit_pos = self.actions.hit_pos
            if not hit_pos:
                self.rfcontext.undo_cancel()
                return 'main'
            if not self.sel_edges:
                return 'main'
            bme0,_ = self.rfcontext.nearest2D_edge(edges=self.sel_edges)
            if not bme0: return
            bmv0,bmv2 = bme0.verts
            bme1,bmv1 = bme0.split()
            bme0.select = True
            bme1.select = True
            self.rfcontext.select(bmv1.link_edges)
            if self.nearest_vert and not self.nearest_vert.select:
                self.nearest_vert.merge(bmv1)
                bmv1 = self.nearest_vert
                self.rfcontext.clean_duplicate_bmedges(bmv1)
                for bme in bmv1.link_edges: bme.select &= len(bme.link_faces)==1
                bme01,bme12 = bmv0.shared_edge(bmv1),bmv1.shared_edge(bmv2)
                if len(bme01.link_faces) == 1: bme01.select = True
                if len(bme12.link_faces) == 1: bme12.select = True
            else:
                bmv1.co = hit_pos
            self.mousedown = self.actions.mousedown
            self.rfcontext.select(bmv1, only=False)
            xy = self.rfcontext.Point_to_Point2D(bmv1.co)
            if not xy:
                dprint('Could not insert: ' + str(bmv1.co))
                self.rfcontext.undo_cancel()
                return 'main'
            self.prep_move(
                bmverts=[bmv1],
                action_confirm=(lambda: self.actions.released('insert')),
            )
            return 'move'

        nearest_edge,d = self.rfcontext.nearest2D_edge(edges=self.vis_edges)
        bmv = self.rfcontext.new2D_vert_mouse()
        if not bmv:
            self.rfcontext.undo_cancel()
            return 'main'
        if d is not None and d < self.rfcontext.drawing.scale(options['polypen insert dist']):
            bme0,bmv2 = nearest_edge.split()
            bmv.merge(bmv2)
        self.rfcontext.select(bmv)
        self.mousedown = self.actions.mousedown
        xy = self.rfcontext.Point_to_Point2D(bmv.co)
        if not xy:
            dprint('Could not insert: ' + str(bmv.co))
            self.rfcontext.undo_cancel()
            return 'main'
        self.prep_move(
                bmverts=[bmv],
                action_confirm=(lambda: self.actions.released('insert')),
            )
        return 'move'

