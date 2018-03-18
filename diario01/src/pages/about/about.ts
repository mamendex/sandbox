import { Component } from '@angular/core';
import { NavController } from 'ionic-angular';

@Component({
  selector: 'page-about',
  templateUrl: 'about.html'
})

export class AboutPage {

	public event = {
	    data: '1990-02-19',
	    horaInicio: '07:43',
	    horaFim: '1990-02-20'
	  }

	public lista_dor_tipo = ['Pulsante', 'Latejante']

	public lista_dor_tipo2 =
	[
		{
			id: 1,
			value: 'Pulsante'
		},
		{
			id: 2,
			value: 'Latejante'
		},
		{
			id: 3,
			value: 'Pressão'
		},
		{
			id: 4,
			value: 'Queimada'
		},
		{
			id: 5,
			value: 'Fisgada'
		},
		{
			id: 9, 
			value: 'Generalizada'
		}  
	]

  constructor(public navCtrl: NavController) {
  		let dor_tipo = 9;
  }

}
