extends CharacterBody2D

signal health_changed(new_health: int)
signal hitbox(area)

func take_damage(amount: int) -> void:
	health_changed.emit(amount)
