# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTIBILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

#################################################

# -------------------
# Tribes DTS Tools
# -------------------
# Add-on: Tribes DTS Import/Export
# Author: Noxwizard, Krogoth, and Contributors
# Description: Import and Export Starsiege: Tribes DTS files.

#################################################

bl_info = {
    "name" : "Tribes DTS Format",
    "author" : "Noxwizard, Krogoth, and Contributors",
    "description" : "Import and Export Starsiege: Tribes DTS files.",
    "blender" : (3, 0, 0),
    "version" : (1, 0, 1),
    "location" : "File > Import-Export",
    "warning" : "",
    "wiki_url" : "https://github.com/tekrog/TribesToBlender",
    "tracker_url" : "https://github.com/tekrog/TribesToBlender/issues",
    "category" : "Import-Export"
}

import bpy
from .main import ImportDTS
from .export_dts import ExportDTS

def menu_func_import(self, context):
    self.layout.operator(ImportDTS.bl_idname, text="Tribes DTS (.dts)")

def menu_func_export(self, context):
    self.layout.operator(ExportDTS.bl_idname, text="Tribes DTS (.dts)")

def register():
    bpy.utils.register_class(ImportDTS)
    bpy.utils.register_class(ExportDTS)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)

def unregister():
    bpy.utils.unregister_class(ImportDTS)
    bpy.utils.unregister_class(ExportDTS)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
