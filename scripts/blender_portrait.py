"""
Blender script: 3D floating card with Hector's portrait.
Run: blender --background --python scripts/blender_portrait.py
"""
import bpy
import math

# Clear scene
bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete()

# Settings
PHOTO = "/Users/hector/Projects/Dr.-strange/hyperframes-test/pachano-showcase/assets/hector-builder.jpg"
OUTPUT = "/Users/hector/Projects/Dr.-strange/renders/hector-3d-card-builder.png"
RES_X, RES_Y = 1920, 1080

# ---- Background ----
world = bpy.data.worlds["World"]
world.use_nodes = True
bg = world.node_tree.nodes["Background"]
bg.inputs["Color"].default_value = (0.02, 0.02, 0.04, 1)  # dark navy
bg.inputs["Strength"].default_value = 1.0

# ---- Photo Card ----
bpy.ops.mesh.primitive_plane_add(size=1, location=(0, 0, 0))
card = bpy.context.active_object
card.name = "PhotoCard"
card.scale = (0.8, 1.1, 1)
card.rotation_euler = (math.radians(5), math.radians(-15), math.radians(3))

# Bevel modifier for rounded corners
bevel = card.modifiers.new("Bevel", "BEVEL")
bevel.width = 0.03
bevel.segments = 8

# Material with photo texture
mat = bpy.data.materials.new("PhotoMat")
mat.use_nodes = True
nodes = mat.node_tree.nodes
links = mat.node_tree.links
nodes.clear()

output = nodes.new("ShaderNodeOutputMaterial")
principled = nodes.new("ShaderNodeBsdfPrincipled")
tex = nodes.new("ShaderNodeTexImage")
tex.image = bpy.data.images.load(PHOTO)
coord = nodes.new("ShaderNodeTexCoord")
mapping = nodes.new("ShaderNodeMapping")

links.new(coord.outputs["UV"], mapping.inputs["Vector"])
links.new(mapping.outputs["Vector"], tex.inputs["Vector"])
links.new(tex.outputs["Color"], principled.inputs["Base Color"])
principled.inputs["Roughness"].default_value = 0.15
principled.inputs["Specular IOR Level"].default_value = 0.3
links.new(principled.outputs["BSDF"], output.inputs["Surface"])
card.data.materials.append(mat)

# ---- Accent glow plane behind card ----
bpy.ops.mesh.primitive_plane_add(size=1, location=(0, 0, -0.05))
glow = bpy.context.active_object
glow.name = "GlowPlane"
glow.scale = (0.85, 1.15, 1)
glow.rotation_euler = card.rotation_euler

glow_mat = bpy.data.materials.new("GlowMat")
glow_mat.use_nodes = True
gn = glow_mat.node_tree.nodes
gl = glow_mat.node_tree.links
gn.clear()
gout = gn.new("ShaderNodeOutputMaterial")
emission = gn.new("ShaderNodeEmission")
emission.inputs["Color"].default_value = (1.0, 0.29, 0.17, 1)  # #FF4B2B accent red
emission.inputs["Strength"].default_value = 3.0
gl.new(emission.outputs["Emission"], gout.inputs["Surface"])
glow.data.materials.append(glow_mat)

# ---- "Pd" text ----
bpy.ops.object.text_add(location=(-0.5, -0.9, 0.02))
txt = bpy.context.active_object
txt.data.body = "Pachano Design"
txt.data.size = 0.12
txt.data.align_x = "LEFT"
txt.rotation_euler = card.rotation_euler

txt_mat = bpy.data.materials.new("TextMat")
txt_mat.use_nodes = True
tn = txt_mat.node_tree.nodes
tl = txt_mat.node_tree.links
tn.clear()
tout = tn.new("ShaderNodeOutputMaterial")
temit = tn.new("ShaderNodeEmission")
temit.inputs["Color"].default_value = (0.9, 0.88, 0.85, 1)  # warm white
temit.inputs["Strength"].default_value = 2.0
tl.new(temit.outputs["Emission"], tout.inputs["Surface"])
txt.data.materials.append(txt_mat)

# ---- Floating particles (small spheres) ----
import random
random.seed(42)
particle_mat = bpy.data.materials.new("ParticleMat")
particle_mat.use_nodes = True
pn = particle_mat.node_tree.nodes
pl = particle_mat.node_tree.links
pn.clear()
pout = pn.new("ShaderNodeOutputMaterial")
pemit = pn.new("ShaderNodeEmission")
pemit.inputs["Color"].default_value = (1.0, 0.29, 0.17, 1)
pemit.inputs["Strength"].default_value = 5.0
pl.new(pemit.outputs["Emission"], pout.inputs["Surface"])

for i in range(20):
    bpy.ops.mesh.primitive_uv_sphere_add(
        radius=random.uniform(0.005, 0.015),
        location=(random.uniform(-1.5, 1.5), random.uniform(-1.5, 1.5), random.uniform(-0.3, 0.3))
    )
    p = bpy.context.active_object
    p.data.materials.append(particle_mat)

# ---- Camera ----
bpy.ops.object.camera_add(location=(0, -2.5, 0.3))
cam = bpy.context.active_object
cam.rotation_euler = (math.radians(83), 0, 0)
cam.data.lens = 50
cam.data.dof.use_dof = True
cam.data.dof.focus_distance = 2.5
cam.data.dof.aperture_fstop = 2.8
bpy.context.scene.camera = cam

# ---- Lights ----
# Key light
bpy.ops.object.light_add(type='AREA', location=(1.5, -1.5, 1.5))
key = bpy.context.active_object
key.data.energy = 150
key.data.size = 1.5
key.data.color = (1, 0.95, 0.9)

# Fill light
bpy.ops.object.light_add(type='AREA', location=(-1.5, -1, 0.5))
fill = bpy.context.active_object
fill.data.energy = 50
fill.data.size = 2
fill.data.color = (0.7, 0.8, 1.0)

# Rim light
bpy.ops.object.light_add(type='SPOT', location=(0, 1, 1))
rim = bpy.context.active_object
rim.data.energy = 200
rim.data.spot_size = math.radians(45)
rim.data.color = (1.0, 0.29, 0.17)
rim.rotation_euler = (math.radians(-45), 0, 0)

# ---- Render settings ----
scene = bpy.context.scene
scene.render.engine = 'CYCLES'
scene.cycles.samples = 128
scene.cycles.use_denoising = True
scene.render.resolution_x = RES_X
scene.render.resolution_y = RES_Y
scene.render.film_transparent = False
scene.render.filepath = OUTPUT
scene.render.image_settings.file_format = 'PNG'

# Use GPU if available
prefs = bpy.context.preferences.addons.get("cycles")
if prefs:
    prefs.preferences.compute_device_type = 'METAL'
    bpy.context.scene.cycles.device = 'GPU'

# Render
bpy.ops.render.render(write_still=True)
print(f"Render saved to: {OUTPUT}")
