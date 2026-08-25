extends CharacterBody2D

signal health_changed(new_health: int)

var speed: float = 200.0
var health: int = 100


func _physics_process(delta: float) -> void:
	var direction := Input.get_axis("move_left", "move_right")
	velocity.x = direction * speed
	move_and_slide()


func take_damage(amount: int) -> void:
	health = max(0, health - amount)
	health_changed.emit(health)
